# Build an ActionExecutor

`ActionExecutor` is the extension point for real-world actions. A new executor
inherits the existing rail:

1. An agent proposes a payload.
2. The executor validates it before it is stored.
3. A human submits and approves the action out of band.
4. The server executes the action.
5. The outcome is audited and exposed through the action status API.

The model never gets to approve its own action. Do not put secrets or real PHI
in cookbook examples, tests, fixtures, logs, or demo payloads.

## The Interface

Every rail implements three methods and two attributes:

```python
class ActionExecutor(Protocol):
    kind: str
    required_env: tuple

    def validate(self, payload: dict) -> list: ...
    def execute(self, action) -> ExecutionResult: ...
    def reconcile(self, action) -> ExecutionResult: ...
```

- `kind` is the public action kind accepted by `/r6/actions/propose`.
- `required_env` lists provider settings checked by `/r6/ops/preflight`.
- `validate()` returns `[]` for a usable payload or error codes such as
  `errors.PAYLOAD_INVALID`.
- `execute()` is called only after commit and out-of-band human approval.
- `reconcile()` asks the provider for truth when an async result is uncertain.

## Worked Toy Executor

`r6/actions/rails/webhook_poster.py` is the minimal example. It posts a
synthetic payload to `WEBHOOK_POSTER_URL` with a bearer token from
`WEBHOOK_POSTER_TOKEN`.

The important shape is:

```python
class WebhookPosterExecutor:
    kind = 'webhook-poster'
    required_env = ('WEBHOOK_POSTER_URL', 'WEBHOOK_POSTER_TOKEN')

    def validate(self, payload):
        if not payload.get('to') or not payload.get('body'):
            return [errors.PAYLOAD_INVALID]
        return []

    def execute(self, action):
        # Check provider config before any network call.
        # POST the already-approved payload.
        # Return ExecutionResult(status='completed') only after a 2xx response.
```

Provider config is deliberately fail-loud. If the URL or token is missing,
`execute()` returns `provider_not_configured` and does not touch the network.
If the provider times out or returns a 5xx, the result is not converted into a
fake success.

## Registration

Put the rail under `r6/actions/rails/`, expose a `register()` function, and
call it once at module import:

```python
def register():
    register_executor(WebhookPosterExecutor())

register()
```

Then import the module from `r6/actions/rails/__init__.py` and add it to
`register_all()`. Tests call `register_all()` after clearing the registry, so
registration must be idempotent.

Until `VALID_KINDS` is derived from the registry, add the new kind to
`r6/actions/models.py` so `/r6/actions/propose` accepts it.

## Confirm-To-Execute Lifecycle

A client proposes the synthetic action:

```json
{
  "kind": "webhook-poster",
  "payload": {
    "to": "Sandbox receiver",
    "body": "Synthetic cookbook payload only.",
    "metadata": {"fixture": "cookbook"}
  }
}
```

`propose` runs `validate()` and stores a draft. `commit` requires a step-up
token and moves the action to `awaiting_confirmation`; it still does not call
the provider. The human approval endpoint atomically claims the action, writes
the consent record, then calls `execute()`. That is the only provider-call
path.

## Tests

Every registered executor automatically runs through
`tests/actions/test_contract_generic.py`. Add focused tests for provider
request shape and executor-specific failure modes, as
`tests/actions/test_webhook_poster_executor.py` does.

Useful local checks while developing a rail:

```bash
uvx ruff check r6/actions/rails tests/actions
uv run python -m pytest tests/actions/test_contract_generic.py \
  tests/actions/test_webhook_poster_executor.py -v
```

Run the broader Python suite before opening a PR when the rail touches shared
action lifecycle behavior.
