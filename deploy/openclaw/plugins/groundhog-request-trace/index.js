import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { registerRequestTrace } from "./core.js";

export default definePluginEntry({
  id: "groundhog-request-trace",
  name: "Groundhog request trace",
  description: "Private request traces and content-free lifecycle summaries in Gateway logs.",
  register: registerRequestTrace,
});
