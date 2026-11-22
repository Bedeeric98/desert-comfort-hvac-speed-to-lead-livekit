// Aggregate Transitions - collapse the stage-rules table rows into ONE item.
return [{ json: { rows: $input.all().map((i) => i.json).filter((r) => r && r.event) } }];
