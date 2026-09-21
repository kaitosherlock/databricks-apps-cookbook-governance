"""HTTP layer over the ``ucg`` backend.

This package contains the only code allowed to know about HTTP. It adds no
governance logic of its own: every authorisation decision, every validation and
the whole mutation pipeline still live in ``ucg``. What changes here is the
transport - a browser sends JSON instead of Streamlit re-running a script.

Two invariants make that transport safe:

* **The actor is derived from proxy headers on every single request.** Nothing
  the browser sends can name the actor, choose a role, or widen a scope.
* **A Plan never crosses the wire.** Previews live in a server-side store and
  the client only ever holds an opaque ``plan_id``. The change itself - the
  principal, the privileges, the target - is read back from the server's copy,
  so a tampered client cannot apply something other than what it previewed.
"""
