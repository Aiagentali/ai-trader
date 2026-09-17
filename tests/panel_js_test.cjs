const assert = require('node:assert/strict');
const {formatBytes, toModelIds} = require('../app/dashboard/static/panel.js');
assert.equal(formatBytes(null), '—');
assert.equal(formatBytes(1048576), '1 MiB');
assert.deepEqual(toModelIds({models:['exact/id',{id:'other/id'},'exact/id']}), ['exact/id','other/id']);
assert.deepEqual(toModelIds({models:null}), []);
console.log('4 panel JavaScript assertions passed');
