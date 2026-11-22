// Aggregate Config - collapse the config table rows into ONE item so the next Data Table node runs once.
return [{ json: { rows: $input.all().map((i) => i.json).filter((r) => r && r.key) } }];
