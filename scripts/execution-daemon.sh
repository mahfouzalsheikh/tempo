#!/bin/sh
# Runs inside Tempo's dedicated Docker-in-Docker service, never on the host.
set -eu

# Keep the wrapper and dockerd on the same firewall implementation.
export DOCKER_IPTABLES_LEGACY=''
mode="${1:-}"

chain() {
    if [ "$mode" = --check ]; then
        "$1" -w 5 -S "$2" >/dev/null
    else
        "$1" -w 5 -S "$2" >/dev/null 2>&1 || "$1" -w 5 -N "$2"
    fi
}

rule() {
    tool="$1"
    position="$2"
    shift 2
    if [ "$mode" = --check ]; then
        "$tool" -w 5 -C "$@"
    elif ! "$tool" -w 5 -C "$@" 2>/dev/null; then
        "$tool" -w 5 "$position" "$@"
    fi
}

if [ "$mode" = --network ]; then
    if ! docker network inspect tempo-agents >/dev/null 2>&1; then
        docker network create --driver bridge \
            --opt com.docker.network.bridge.name=tempo-agents0 \
            --opt com.docker.network.bridge.enable_icc=false \
            --label tempo.execution-policy=public-web-v1 tempo-agents
    fi
    actual="$(docker network inspect tempo-agents --format '{{.Driver}} {{.EnableIPv6}} {{index .Options "com.docker.network.bridge.name"}} {{index .Options "com.docker.network.bridge.enable_icc"}} {{index .Labels "tempo.execution-policy"}}')"
    test "$actual" = 'bridge false tempo-agents0 false public-web-v1'
    exit
fi

chain iptables DOCKER-USER
chain iptables TEMPO-JOB-OUT
chain iptables TEMPO-JOB-IN
chain ip6tables TEMPO-JOB6

# No workload may reach private networks, loopback, metadata, or reserved destinations.
for destination in \
    0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 \
    172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 192.88.99.0/24 192.168.0.0/16 \
    198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 224.0.0.0/4 240.0.0.0/4; do
    rule iptables -A TEMPO-JOB-OUT -d "$destination" -j REJECT
done
rule iptables -A TEMPO-JOB-OUT -p tcp -m multiport --dports 80,443 -j RETURN
for resolver in 1.1.1.1 8.8.8.8; do
    for protocol in udp tcp; do
        rule iptables -A TEMPO-JOB-OUT -d "$resolver" -p "$protocol" --dport 53 -j RETURN
    done
done
rule iptables -A TEMPO-JOB-OUT -j REJECT
# Daemon-local traffic does not traverse DOCKER-USER; protect INPUT separately.
rule iptables -A TEMPO-JOB-IN -j REJECT
# IPv6 is not an execution capability in this policy.
rule ip6tables -A TEMPO-JOB6 -j REJECT
for interface in docker0 br+ tempo-agents0; do
    rule iptables -I INPUT -i "$interface" -j TEMPO-JOB-IN
    rule iptables -I DOCKER-USER -i "$interface" -j TEMPO-JOB-OUT
    rule ip6tables -I INPUT -i "$interface" -j TEMPO-JOB6
    rule ip6tables -I FORWARD -i "$interface" -j TEMPO-JOB6
done

# Wire forwarding before dockerd starts, rather than waiting for Docker to add its hook.
rule iptables -I FORWARD -j DOCKER-USER

if [ "$mode" = --check ]; then
    docker info >/dev/null
    exit
fi

# Policy exists before dockerd begins accepting jobs, including after a restart.
exec /usr/local/bin/dockerd-entrypoint.sh "$@"
