# Phase 2: Disposable JavaScript runtime

Implement one disposable child execution unit per run with a fake host capability endpoint. The trusted parent owns deadline and termination.

Generated code receives only computation and a narrow asynchronous `tools.call(capability, arguments)` interface. It must not receive credentials or ambient filesystem, network, process, shell or package-loading authority by contract.

Bound stdout, stderr, final result, runtime and message sizes. Add adversarial tests for infinite loops, environment/credential discovery, filesystem/network/process/package attempts, oversized output and malformed return values. Native OS sandbox enforcement is Phase 6; do not claim stronger containment than implemented.
