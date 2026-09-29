const assert = require('node:assert/strict');
const {formatBytes, toModelIds, filterLogs, summariseDiagnostics} = require('../app/dashboard/static/panel.js');

assert.equal(formatBytes(null), '—');
assert.equal(formatBytes(1048576), '1 MiB');
assert.deepEqual(toModelIds({models:['exact/id',{id:'other/id'},'exact/id']}), ['exact/id','other/id']);
assert.deepEqual(toModelIds({models:null}), []);

const logs = [
  '{"ts":"1","level":"INFO","msg":"agent starting"}',
  '{"ts":"2","level":"WARNING","msg":"spread abnormal"}',
  '{"ts":"3","level":"ERROR","msg":"AI call failed"}',
  'plain line without a level',
];
assert.deepEqual(filterLogs(logs, 'ERROR').length, 2); // the ERROR + the unlabelled line
assert.deepEqual(filterLogs(logs, 'ERROR')[0], logs[2]);
assert.deepEqual(filterLogs(logs, 'WARN').length, 3);
assert.equal(filterLogs(logs, 'all').length, 4);
assert.deepEqual(filterLogs(null, 'ERROR'), []);
assert.deepEqual(filterLogs(logs, 'BOGUS').length, 4);

const rows = summariseDiagnostics({
  ai_by_model: [
    {model:'good/model', calls:10, failed:0, last_seen:'x'},
    {model:'bad/model', calls:4, failed:3, last_seen:'x'},
  ],
  ai_by_status: {ok: 11, error: 3},
  recovered: [{symbol:'EURUSD'}],
  decisions_by_kind: {WAIT: 20, RECOVERED: 3, PARSE_ERROR: 1},
});
assert.equal(rows.length, 2 + 1 + 1 + 3); // 2 models + 1 bad status + recovered + 3 decisions
const badModel = rows.find(r => r.label.includes('bad/model'));
assert.match(badModel.value, /3 خطا \(75٪\)/);
assert.equal(badModel.bad, true);
assert.equal(rows.find(r => r.label === 'مدل good/model').bad, false);
assert.equal(rows.find(r => r.label === 'وضعیت درخواست‌ها').bad, true);
assert.equal(rows.find(r => r.label === 'تصمیم WAIT').bad, false);
assert.equal(rows.find(r => r.label === 'تصمیم PARSE_ERROR').bad, true);
assert.deepEqual(summariseDiagnostics(null), []);

console.log('panel JavaScript assertions passed');
