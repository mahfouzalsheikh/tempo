import copy

from django.test import Client

from tempo.intake import revise_plan
from tempo.product_execution import enqueue_product
from tests.test_product_execution import arguments, factory  # noqa: F401


def test_progress_is_private_read_only_and_pinned_to_the_reviewed_plan(factory):  # noqa: F811
    run = enqueue_product(factory.store, factory.product.pk, **arguments(factory))
    path = f"/ideas/{factory.product.pk}/progress/"
    client = Client(enforce_csrf_checks=True)
    assert client.get(path).status_code == 302
    client.force_login(factory.user)
    assert client.get(path).status_code == 400
    assert client.get(path, {"revision": "no", "plan": 1}).status_code == 400
    response = client.get(path, {"revision": 1, "plan": 1})
    assert response.status_code == 200
    assert b'data-poll="true"' in response.content
    assert f"Run #{run.pk} ".encode() in response.content
    assert "no-store" in response["Cache-Control"]
    assert "Cookie" in response["Vary"]
    token = client.cookies["csrftoken"].value
    assert client.post(path, HTTP_X_CSRFTOKEN=token).status_code == 405
    assert client.get(path, {"revision": 1, "plan": 99}).status_code == 404
    updated = copy.deepcopy(factory.plan.specification)
    updated["tasks"][0]["instructions"] += " Review a changed interface."
    revise_plan(
        factory.product.pk, expected_plan_id=factory.plan.pk,
        specification=updated, user_id=factory.user.pk,
    )
    assert f"Run #{run.pk} ".encode() in client.get(
        path, {"revision": 1, "plan": 1},
    ).content
    assert f"Run #{run.pk} ".encode() not in client.get(
        path, {"revision": 1, "plan": 2},
    ).content


def test_stopped_progress_explains_checkout_failure_without_rendering_error_html(factory):  # noqa: F811
    run = enqueue_product(factory.store, factory.product.pk, **arguments(factory))
    run.status = "failed"
    run.error = "after_create hook exited 128: Authentication failed for <script>bad()</script>"
    run.save(update_fields=["status", "error"])
    client = Client()
    client.force_login(factory.user)
    response = client.get(f"/ideas/{factory.product.pk}/")
    body = response.content.decode()
    assert body.index('id="execution"') < body.index('THE BRIEF ·')
    assert 'data-poll="false"' in body
    assert "Repository access needs attention." in body
    assert "Retry saved execution" in body
    assert "<script>bad()" not in body
    assert "&lt;script&gt;bad()" in body
    run.status = "running"
    run.save(update_fields=["status"])
    body = client.get(f"/ideas/{factory.product.pk}/").content.decode()
    assert "Previous attempt error" in body
    assert "Repository access needs attention." not in body
