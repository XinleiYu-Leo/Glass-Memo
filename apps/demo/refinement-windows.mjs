// Experimental only. Keep new images plus a previously seen frame on each side.
// Frame reuse means already inspected, not independently rechecked by the model.
export function contextDeduplicatedWindows(windows, previouslySeenIDs) {
  const seen = new Set(previouslySeenIDs);
  const result = [];
  for (const window of windows) {
    const fresh = window.filter(frame => !seen.has(frame.frame_id));
    if (!fresh.length) continue;
    const first = fresh[0].output_pts_seconds, last = fresh.at(-1).output_pts_seconds;
    const before = window.filter(frame => seen.has(frame.frame_id) && frame.output_pts_seconds < first).at(-1);
    const after = window.find(frame => seen.has(frame.frame_id) && frame.output_pts_seconds > last);
    const retained = new Set([...fresh, ...(before ? [before] : []), ...(after ? [after] : [])].map(frame => frame.frame_id));
    const selected = window.filter(frame => retained.has(frame.frame_id));
    result.push(selected);
    for (const frame of selected) seen.add(frame.frame_id);
  }
  return result;
}
