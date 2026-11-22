"use strict";

const vm = require("node:vm");
const readline = require("node:readline");

async function main() {
    const input = readline.createInterface({ input: process.stdin, terminal: false });
    const lines = input[Symbol.asyncIterator]();
    const first = await lines.next();
    if (first.done) return;
    const { source, messageBytes } = JSON.parse(first.value);
    const context = vm.createContext(Object.create(null), {
        codeGeneration: { strings: false, wasm: false },
    });

    // Only guest-realm functions are exposed. The driver exchanges strings
    // with the controller and never injects host objects into the context.
    const bootstrap = function (limit) {
        "use strict";
        const encode = JSON.stringify;
        const decode = JSON.parse;
        const apply = Reflect.apply;
        const owns = Object.hasOwn;
        const finite = Number.isFinite;
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
                        if (!finite(item)) throw new ErrorType();
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
            const wire = serialize({ __proto__: null, type: "call", capability, args });
            // A getter invoked during serialization could reenter tools.call.
            if (final || fatal || pending) throw new ErrorType("Concurrent capability call");
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
                    final = serialize({ __proto__: null, type: "result", result });
                } catch {
                    fatal = true;
                }
            },
            next() {
                if (fatal) return '{"type":"error"}';
                if (pending && pending.wire) {
                    const wire = pending.wire;
                    pending.wire = undefined;
                    return wire;
                }
                return final;
            },
            reply(raw) {
                const current = pending;
                pending = undefined;
                const reply = decode(raw);
                if (owns(reply, "result")) current.resolve(reply.result);
                else current.reject(new ErrorType("Capability call denied"));
            }
        });
    };
    const controller = vm.runInContext("(" + bootstrap + ")(" + messageBytes + ")", context);

    try {
        const program = new vm.Script('(async () => { "use strict";\n' + source + '\n})', {
            filename: "generated.js",
            importModuleDynamically: controller.denyImport,
        }).runInContext(context);
        controller.start(program);
    } catch {
        process.stdout.write('{"type":"error"}\n');
        return;
    }

    for (;;) {
        await new Promise(resolve => setTimeout(resolve, 1));
        const wire = controller.next();
        if (wire === undefined) continue;
        if (Buffer.byteLength(wire) > messageBytes) {
            process.stdout.write('{"type":"error"}\n');
            return;
        }
        if (!process.stdout.write(wire + "\n")) {
            await new Promise(resolve => process.stdout.once("drain", resolve));
        }
        if (JSON.parse(wire).type !== "call") return;
        const reply = await lines.next();
        if (reply.done) return;
        controller.reply(reply.value);
    }
}

main().then(() => process.stdin.destroy(), () => {
    process.exitCode = 1;
    process.stdin.destroy();
});
