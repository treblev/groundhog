/** Diagnostic only: exercise an installed OpenClaw renderer with a fake Gateway.
 * No Telegram, iCloud, or live Gateway calls are made. Input is an elicitation
 * request containing synthetic event details, read as JSON from stdin.
 */
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const { values } = parseArgs({ options: { "openclaw-root": { type: "string" } } });
assert.ok(values["openclaw-root"], "Supply --openclaw-root for the installed OpenClaw package");
const root = values["openclaw-root"];
const dist = join(root, "dist");
const names = await readdir(dist);

async function exportedFunction(prefix, functionName) {
  for (const name of names.filter((entry) => entry.startsWith(prefix) && entry.endsWith(".mjs"))) {
    const path = join(dist, name);
    if (!(await readFile(path, "utf8")).includes(`function ${functionName}(`)) continue;
    const module = await import(pathToFileURL(path).href);
    const fn = Object.values(module).find((value) => typeof value === "function" && value.name === functionName);
    if (fn) return fn;
  }
  throw new Error(`Installed runtime does not expose the expected ${functionName} implementation; inspect this OpenClaw version`);
}

const createHandler = await exportedFunction("mcp-client-elicitation-", "createMcpClientElicitationHandler");
const buildPrompt = await exportedFunction("gateway-question-", "buildAgentHarnessQuestionPromptPayload");
let input = "";
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
assert.deepEqual(request.requestedSchema.properties, {});
const outcomes = [];

for (const answer of ["Allow", "Decline", "expired"]) {
  let captured;
  const calls = [];
  const gatewayCall = async (method, _options, args) => {
    calls.push(method);
    if (method === "question.request") { captured = args; return { id: args.id }; }
    if (method === "question.waitAnswer") {
      if (answer === "expired") return { id: args.id, status: "expired" };
      return { id: args.id, status: "answered", answers: {
        answers: { [captured.questions[0].questionId]: [answer] },
      } };
    }
    if (method === "question.resolve") return { id: args.id };
    throw new Error(`Unexpected Gateway operation in isolated test: ${method}`);
  };
  const handler = createHandler({
    sessionKey: `agent:main:calendar-renderer-probe-${answer.toLowerCase()}`,
    agentId: "main", gatewayCall, assertCurrent() {},
  });
  const result = await handler({ method: "elicitation/create", requestId: "synthetic-test",
    params: request, signal: new AbortController().signal });
  const prompt = buildPrompt({ questionId: captured.id, questions: captured.questions,
    options: { intro: request.message } });
  const nativeText = prompt.presentation.blocks.filter((block) => block.type === "text")
    .map((block) => block.text).join("\n");
  assert.ok(nativeText.includes(request.message), "The native confirmation card hides operation details");
  if (answer === "expired") assert.equal(result.action, "cancel");
  else if (answer === "Allow") {
    assert.equal(result.action, "accept");
    assert.deepEqual(result.content, {});
  } else assert.equal(result.action, "decline");
  outcomes.push({ answer, result, nativePreviewVisible: true, gatewayCallsStubbed: calls });
}

const metadata = JSON.parse(await readFile(join(root, "package.json"), "utf8"));
process.stdout.write(JSON.stringify({ openclawVersion: metadata.version,
  transport: "fake Gateway; no messages or calendar requests", outcomes }, null, 2) + "\n");
