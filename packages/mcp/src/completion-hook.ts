/** Installed as a self-contained local Claude Code Stop hook. No network or transcript access. */
export const COMPLETION_HOOK = `
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  try {
    const event = JSON.parse(input);
    if (event.hook_event_name !== 'Stop' || event.stop_hook_active) return;
    if (Array.isArray(event.background_tasks) && event.background_tasks.length) return;
    process.stdout.write(JSON.stringify({
      decision: 'block',
      reason: "CrowdCode completion check (once only): read crowdcode_status. If off, unavailable, or this turn only managed CrowdCode settings/reviews, finish immediately without reflection or submissions. Otherwise, if you have not already reflected for this completed task, consider the real failures, poor results, excessive cost, and detours: what specific reusable service would have been worth paying for? Call request_service only for worthwhile unmet needs, describing exact inputs, deliverables, acceptance criteria, the real obstacle, and why the outcome justifies payment. A specific improvement to an inadequate existing service qualifies; no actual purchase is needed. Skip local Python/runtime wishes and services that already worked well. Never invent a budget or repeat a submitted gap. If nothing qualifies, submit nothing. Then finish; do not reopen the user's task."
    }));
  } catch { /* A broken optional reminder must never trap the user. */ }
});
`;

