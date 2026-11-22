"use strict";

const vm = require("node:vm");
const readline = require("node:readline");

async function main() {
    const input = readline.createInterface({ input: process.stdin, terminal: false });
    const lines = input[Symbol.asyncIterator]();
    const first = await lines.next();
    if (first.done) return;
    const { type, version, run_id, source, messageBytes } = JSON.parse(first.value);
    if (type !== "start" || version !== 1 || typeof run_id !== "string" ||
        typeof source !== "string" || messageBytes !== 64 * 1024) {
        throw new Error("Invalid runtime startup message");
    }
    const failure = JSON.stringify({ version, run_id, type: "error", code: "EXECUTION_FAILED" });
    const context = vm.createContext(Object.create(null), {
        codeGeneration: { strings: false, wasm: false },
    });

    // Only guest-realm functions are exposed. The driver exchanges strings
    // with the controller and never injects host objects into the context.
    const bootstrap = function (limit, version, run_id, failure) {
        "use strict";
        const encode = JSON.stringify;
        const decode = JSON.parse;
        const apply = Reflect.apply;
        const owns = Object.hasOwn;
        const finite = Number.isFinite;
        const abs = Math.abs;
        const maxSafeInteger = Number.MAX_SAFE_INTEGER;
        const isArray = Array.isArray;
        const prototypeOf = Object.getPrototypeOf;
        const setPrototypeOf = Object.setPrototypeOf;
        const objectPrototype = Object.prototype;
        const arrayPrototype = Array.prototype;
        const ownKeys = Reflect.ownKeys;
        const descriptor = Object.getOwnPropertyDescriptor;
        const toString = String;
        const PromiseType = Promise;
        const ErrorType = Error;
        let callId = 0;
        let pending;
        let final;
        let fatal;

        function serialize(value) {
            let budget = limit;
            function snapshot(item, depth) {
                if (--budget < 0 || depth > 64) throw new ErrorType();
                switch (typeof item) {
                    case "string":
                        budget -= item.length;
                        if (budget < 0) throw new ErrorType();
                        return item;
                    case "undefined":
                    case "symbol":
                    case "function":
                    case "bigint":
                        throw new ErrorType();
                    case "number":
                        if (!finite(item) || abs(item) > maxSafeInteger) throw new ErrorType();
                        return item;
                    case "boolean":
                        return item;
                }
                if (item === null) return null;
                const array = isArray(item);
                const prototype = prototypeOf(item);
                if (array ? prototype !== arrayPrototype :
                    prototype !== objectPrototype && prototype !== null) {
                    throw new ErrorType();
                }
                const result = array ? setPrototypeOf([], null) : { __proto__: null };
                const keys = ownKeys(item);
                const count = keys.length - (array ? 1 : 0);
                if (count > budget || (array && descriptor(item, "length").value !== count)) {
                    throw new ErrorType();
                }
                for (let index = 0; index < keys.length; index++) {
                    const key = keys[index];
                    if (array && key === "length") continue;
                    if (typeof key !== "string" || (array && key !== toString(index))) {
                        throw new ErrorType();
                    }
                    budget -= key.length;
                    const property = descriptor(item, key);
                    if (budget < 0 || !property || !property.enumerable || !owns(property, "value")) {
                        throw new ErrorType();
                    }
                    result[key] = snapshot(property.value, depth + 1);
                }
                return result;
            }
            const wire = encode(snapshot(value, 0));
            if (wire.length > limit) throw new ErrorType();
            return wire;
        }

        const tools = Object.freeze({ async call(capability, args) {
            if (final || fatal || pending || typeof capability !== "string") {
                throw new ErrorType("Invalid or concurrent capability call");
            }
            const nextId = callId + 1;
            const wire = serialize({
                __proto__: null, version, run_id, type: "call", call_id: nextId, capability, args,
            });
            // Proxy traps can reenter tools.call during snapshotting.
            if (final || fatal || pending) throw new ErrorType("Concurrent capability call");
            callId = nextId;
            return new PromiseType((resolve, reject) => {
                pending = { __proto__: null, wire, resolve, reject };
            });
        } });
        Object.defineProperty(globalThis, "tools", { value: tools });
        Object.defineProperty(globalThis, "console", { value: undefined });

        return Object.freeze({
            denyImport() {
                throw new ErrorType("Module loading is disabled");
            },
            async start(program) {
                try {
                    const result = await apply(program, undefined, []);
                    final = serialize({ __proto__: null, version, run_id, type: "result", result });
                } catch {
                    fatal = true;
                }
            },
            next() {
                if (fatal) return failure;
                if (pending && pending.wire) {
                    const wire = pending.wire;
                    pending.wire = undefined;
                    return wire;
                }
                return final;
            },
            reply(raw) {
                const reply = decode(raw);
                if (reply === null || typeof reply !== "object" || ownKeys(reply).length !== 5 ||
                    !owns(reply, "version") || reply.version !== version ||
                    !owns(reply, "run_id") || reply.run_id !== run_id ||
                    !owns(reply, "call_id") || reply.call_id !== callId || !owns(reply, "type")) {
                    throw new ErrorType("Invalid capability response");
                }
                const current = pending;
                pending = undefined;
                if (reply.type === "call_result" && owns(reply, "result")) {
                    current.resolve(reply.result);
                } else if (reply.type === "call_error" && owns(reply, "code") &&
                    (reply.code === "DENIED" || reply.code === "RESULT_TOO_LARGE")) {
                    current.reject(new ErrorType(reply.code === "DENIED" ?
                        "Capability call denied" : "Capability response too large"));
                } else {
                    throw new ErrorType("Invalid capability response");
                }
            }
        });
    };
    const bootstrapArgs = [messageBytes, version, run_id, failure].map(value => JSON.stringify(value));
    const controller = vm.runInContext("(" + bootstrap + ")(" + bootstrapArgs.join(",") + ")", context);

    try {
        const program = new vm.Script('(async () => { "use strict";\n' + source + '\n})', {
            filename: "generated.js",
            importModuleDynamically: controller.denyImport,
        }).runInContext(context);
        controller.start(program);
    } catch {
        process.stdout.write(failure + "\n");
        return;
    }

    for (;;) {
        await new Promise(resolve => setTimeout(resolve, 1));
        const wire = controller.next();
        if (wire === undefined) continue;
        if (Buffer.byteLength(wire) > messageBytes) {
            process.stdout.write(failure + "\n");
            return;
        }
        if (!process.stdout.write(wire + "\n")) {
            await new Promise(resolve => process.stdout.once("drain", resolve));
        }
        if (JSON.parse(wire).type !== "call") return;
        const reply = await lines.next();
        if (reply.done) return;
        if (Buffer.byteLength(reply.value) > messageBytes) {
            throw new Error("Oversized capability response");
        }
        controller.reply(reply.value);
    }
}

main().then(() => process.stdin.destroy(), () => {
    process.exitCode = 1;
    process.stdin.destroy();
});
