// Normalize Claim - fail-open duplicate check (same design as the call-end guard): only POSITIVE evidence of a duplicate
// (done mark present, or the lease already held) drops an event. Redis down or odd output lets the event through.
const done = $('Check Event Done (Redis)').first().json.doneValue;
const lease = $('Claim Event Lease (Redis)').first().json;
const nums = Object.values(lease || {}).filter((v) => typeof v === 'number');
const leaseCount = nums.length ? nums[0] : null;
const alreadyDone = done === 'true' || done === true;
const leaseTaken = leaseCount !== null && leaseCount > 1;
return [{ json: { is_new: !(alreadyDone || leaseTaken), already_done: alreadyDone, lease_count: leaseCount } }];
