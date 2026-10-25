# Execution semantics

Wyrit separates the lifecycle of generated computation from the lifecycle of external actions.

## Run states

`RUNNING -> SUCCESS | FAILED | TIMED_OUT | CANCELLED`

A run deadline is owned by the trusted controller outside generated JavaScript.

## Action states

`CREATED -> DISPATCHED -> SUCCEEDED | FAILED | UNKNOWN`

UNKNOWN is required when dispatch is established but completion cannot be established. Killing the execution unit cannot recall a request already accepted by another service.

## Cancellation

Cancellation stops admission of new calls and requests termination of the execution unit. It must distinguish cancellation requested from cancellation confirmed. Downstream cancellation is capability-specific and must never be assumed.

## Partial execution

Completed and dispatched actions survive later run failure. Structured outcomes preserve action records.

## Retry

No automatic retry of non-idempotent actions without an explicit safety mechanism such as a downstream idempotency key with defined semantics.

## Bounds

Runtime, call count, concurrency, IPC message size, broker response size, final output, stdout and stderr are bounded. Memory/process limits are enforced where the platform contract claims them.
