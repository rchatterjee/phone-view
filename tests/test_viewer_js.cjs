// Run: node tests/test_viewer_js.cjs
const assert = require('node:assert/strict');
const path = require('node:path');
const v = require(path.join(__dirname, '..', 'src', 'phoneview', 'static', 'viewer.js'));

// Coordinates map the drawn rectangle to 0..65535 and clamp outside it.
const rect = {left: 100, top: 50, width: 200, height: 400};
assert.deepEqual(v.normalise(100, 50, rect), {x: 0, y: 0});
assert.deepEqual(v.normalise(300, 450, rect), {x: 65535, y: 65535});
assert.deepEqual(v.normalise(200, 250, rect), {x: 32768, y: 32768});
assert.deepEqual(v.normalise(-5, 9999, rect), {x: 0, y: 65535});

// Binary framing matches session.py.
const video = new ArrayBuffer(13);
const dv = new DataView(video);
dv.setUint8(0, 1); dv.setUint8(1, 1); dv.setBigUint64(2, 123456n);
new Uint8Array(video, 10).set([7, 8, 9]);
const parsed = v.parseBinary(video);
assert.equal(parsed.type, 'video'); assert.equal(parsed.key, true); assert.equal(parsed.pts, 123456);
assert.deepEqual([...parsed.data], [7, 8, 9]);
const jpeg = new Uint8Array([2, 0xff, 0xd8]).buffer;
assert.deepEqual([...v.parseBinary(jpeg).data], [0xff, 0xd8]);

// Keys only map when the backend supports them; Escape becomes Back on Android.
const ev = (key, extra = {}) => ({key, ctrlKey: false, metaKey: false, altKey: false, ...extra});
assert.equal(v.keyForEvent(ev('Enter'), ['enter']), 'enter');
assert.equal(v.keyForEvent(ev('Enter'), []), null);
assert.equal(v.keyForEvent(ev('Escape'), ['back']), 'back');
assert.equal(v.keyForEvent(ev('Enter', {ctrlKey: true}), ['enter']), null);
assert.equal(v.isPrintable(ev('a')), true);
assert.equal(v.isPrintable(ev('a', {ctrlKey: true})), false);
assert.equal(v.isPrintable(ev('Shift')), false);

// Decoder config: Annex-B (no description) vs hvcC (description bytes).
assert.deepEqual(v.decoderConfig({codec_string: 'avc1.42c01f', format: 'annexb'}),
                 {codec: 'avc1.42c01f', optimizeForLatency: true});
const hevc = v.decoderConfig({codec_string: 'hev1.1.6.L93.B0', format: 'hvcc', description: 'aHZjYw=='});
assert.deepEqual([...hevc.description], [...Buffer.from('hvcc')]);

// Pointer slots: mouse is 0, touches get the lowest free slot, single-touch refuses a second finger.
const slots = new v.PointerSlots();
assert.equal(slots.claim(11, false, true), 0);
assert.equal(slots.claim(12, false, true), 1);
assert.equal(slots.release(11), 0);
assert.equal(slots.claim(13, false, true), 0);
const single = new v.PointerSlots();
assert.equal(single.claim(1, true, false), 0);
assert.equal(single.claim(2, false, false), null);

console.log('viewer.js helper tests passed');

// Screenshot reordering.
assert.deepEqual(v.reorderIds(['a', 'b', 'c', 'd'], 'd', 'b', false), ['a', 'd', 'b', 'c']);
assert.deepEqual(v.reorderIds(['a', 'b', 'c', 'd'], 'a', 'c', true), ['b', 'c', 'a', 'd']);
assert.deepEqual(v.reorderIds(['a', 'b'], 'a', 'a', true), ['a', 'b']);
assert.deepEqual(v.reorderIds(['a', 'b'], 'x', 'a', true), ['a', 'b']);
assert.deepEqual(v.stepIds(['a', 'b', 'c'], 'b', -1), ['b', 'a', 'c']);
assert.deepEqual(v.stepIds(['a', 'b', 'c'], 'c', 1), ['a', 'b', 'c']);
console.log('screenshot order tests passed');

// Decoder size: the real picture size goes to WebCodecs, and frames reporting less are corrected.
const tall = {codec_string: 'hev1.1.6.L150.B0', format: 'hvcc', description: 'AAEC',
              width: 1184, height: 2576, coded_width: 1184, coded_height: 2576};
const conf = v.decoderConfig(tall);
assert.equal(conf.codedWidth, 1184); assert.equal(conf.codedHeight, 2576);
assert.equal(conf.displayAspectWidth, 1184); assert.equal(conf.displayAspectHeight, 2576);
assert.equal(v.decoderConfig({codec_string: 'x', format: 'annexb', width: 0, height: 0}).codedWidth, undefined);
const frame = (dw, dh, cw, ch) => ({displayWidth: dw, displayHeight: dh, codedWidth: cw, codedHeight: ch});
assert.deepEqual(v.visibleFix(frame(1280, 720, 1184, 2576), tall), {x: 0, y: 0, width: 1184, height: 2576});
assert.equal(v.visibleFix(frame(1184, 2576, 1184, 2576), tall), null);    // already right
assert.equal(v.visibleFix(frame(1280, 720, 1280, 720), tall), null);      // cannot read outside the decoded picture
assert.equal(v.visibleFix(frame(1280, 720, 1184, 2576), {width: 0, height: 0}), null);
assert.equal(v.viewWidth('fit', 1184), null);
assert.equal(v.viewWidth('50', 1184), 592);
assert.equal(v.viewWidth('100', 0), null);
console.log('decoder size tests passed');
