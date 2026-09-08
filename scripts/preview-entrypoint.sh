#!/bin/sh
set -eu

# These rules affect only this container's network namespace. Install them before
# starting a listener; any failure stops startup. The serving process drops every
# capability, including its bounding set, before reading any preview request.
iptables -w 5 -P OUTPUT DROP
iptables -w 5 -P INPUT DROP
iptables -w 5 -F OUTPUT
iptables -w 5 -F INPUT
iptables -w 5 -A OUTPUT -m conntrack --ctstate ESTABLISHED -j ACCEPT
iptables -w 5 -A OUTPUT -o lo -d 127.0.0.1/32 -j ACCEPT
iptables -w 5 -A INPUT -m conntrack --ctstate ESTABLISHED -j ACCEPT
iptables -w 5 -A INPUT -p tcp --dport 8080 -j ACCEPT
ip6tables -w 5 -P OUTPUT DROP
ip6tables -w 5 -P INPUT DROP

exec setpriv --reuid=10001 --regid=10001 --clear-groups --bounding-set=-all \
    --inh-caps=-all --ambient-caps=-all python -m tempo.preview_server
