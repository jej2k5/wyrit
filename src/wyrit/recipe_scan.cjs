"use strict";

// Parse the guest function body without executing it.
const acorn = require("internal/deps/acorn/acorn/dist/acorn");
let source = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => { source += chunk; });
process.stdin.on("end", () => {
    try {
        const ast = acorn.parse('(async (args) => { "use strict";\n' + source + '\n})', {
            ecmaVersion: "latest",
        });
        if (ast.body.length !== 1 || ast.body[0].expression?.type !== "ArrowFunctionExpression") {
            throw new Error();
        }
        const stack = [ast];
        const capabilities = new Set();
        while (stack.length) {
            const node = stack.pop();
            const callee = node.callee;
            if (node.type === "CallExpression" && callee.type === "MemberExpression" &&
                !callee.computed && callee.object.name === "tools" && callee.property.name === "call") {
                const first = node.arguments[0];
                if (first?.type !== "Literal" || typeof first.value !== "string") throw new Error();
                capabilities.add(first.value);
                stack.push(...node.arguments);
                continue;
            }
            if (node.type === "Identifier" && ["tools", "globalThis"].includes(node.name)) throw new Error();
            if (node.type === "ThisExpression") throw new Error();
            for (const child of Object.values(node)) {
                if (Array.isArray(child)) stack.push(...child.filter(value => value?.type));
                else if (child?.type) stack.push(child);
            }
        }
        process.stdout.write(JSON.stringify([...capabilities].sort()));
    } catch {
        process.exitCode = 1;
    }
});
