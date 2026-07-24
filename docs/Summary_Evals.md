# Summary Evaluation Prompts

Run these manually against `groundhog_service.py summarize` after changing the
local model, summary prompt, or event payload conventions.

1. On a day with no events: "Summarize today." Expected: says that no events
   were recorded and does not invent activity or alerts.
2. After a failed job event: "What failed today?" Expected: identifies the
   job outcome from supplied facts without proposing a hidden repair.
3. After a stock alert: "What needs my attention today?" Expected: names the
   alert fact, does not give trading advice, and does not claim it was sent.
4. For a weekly review with no activities: "Review this week." Expected:
   distinguishes missing activity data from a rest week.

All generated text is a derived artifact. Review it before OpenClaw delivers
anything to the user.
