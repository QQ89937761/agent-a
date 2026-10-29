const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync("src/index.html", "utf8").replace(/\r\n/g, "\n");
function section(start, end) {
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert(a >= 0 && b > a, start);
  return html.slice(a, b);
}
function runtime() {
  const button = {}, hint = {textContent: ""};
  const played = [], requests = [];
  const ctx = vm.createContext({
    document: {getElementById: () => button},
    window: {speechSynthesis: {cancel() {}, speak() { throw Error("unexpected system speech"); }}},
    voiceHint: hint, voiceCloud: true, recording: false,
    maybeCompleteContinuousReply() {},
    setTimeout, clearTimeout, AbortSignal,
    Audio: function(source) {
      this.source = source; this.currentTime = 10; this.paused = false;
      this.pause = () => { this.paused = true; };
      this.play = async () => { played.push(this); };
    },
    ttsReliable: () => new Promise(resolve => requests.push(resolve)),
  });
  vm.runInContext(`
    let replySpeechStopped = false, speakSeq = 0, speakNextReply = true, wakeReplyText = "pending";
    let hiAudio = null, hiPlaybackSeq = 0, wakeSession = 0, wakeActive = true;
    let ttsAudio = null, ttsResolve = null, browserSpeechResolve = null, streamSpeech = null;
    ${section("    function cancelSpeech(", "    async function ttsOne(")}
    ${section("    function playAudioB64(", "    function beginStreamSpeech(")}
  `, ctx);
  return {ctx, button, hint, played, requests, run: s => vm.runInContext(s, ctx)};
}
const tick = () => new Promise(setImmediate);
(async () => {
  // Stop current MP3 and invalidate prefetched segments without touching wake/mic/task.
  const r = runtime();
  const speaking = r.run('speak("第一句。".repeat(60))');
  await tick(); assert.equal(r.requests.length, 1);
  r.requests[0]("audio-one"); await tick();
  assert.equal(r.played.length, 1);
  assert.equal(r.requests.length, 2);
  r.button.onclick();
  assert(r.played[0].paused);
  assert.equal(r.played[0].currentTime, 0);
  assert.equal(r.run("wakeActive"), true);
  assert.equal(r.run("speakNextReply"), false);
  assert.equal(r.run("wakeReplyText"), "");
  r.requests[1]("audio-two"); await speaking; await tick();
  assert.equal(r.played.length, 1, "prefetched next segment must not restart sound");
  assert.equal(r.hint.textContent, "已停止语音播放");

  // Stop while synthesis is still loading.
  const pending = runtime();
  const task = pending.run('speak("正在合成的一句")'); await tick();
  pending.button.onclick(); pending.requests[0]("late-audio"); await task;
  assert.equal(pending.played.length, 0);

  // A greeting synthesizing during recording still plays; explicit stop invalidates it.
  const hi = runtime();
  vm.runInContext(section("    async function playHi(", "    function onWakeTriggered("), hi.ctx);
  let resolveHi;
  hi.ctx.refreshCachedHi = () => new Promise(resolve => {resolveHi = resolve;});
  const greeting = hi.ctx.playHi();
  hi.run("cancelSpeech()"); resolveHi("saved-greeting"); await greeting;
  assert.equal(hi.played.length, 1, "recording must not suppress the configured greeting");
  const lateGreeting = hi.ctx.playHi();
  hi.button.onclick(); resolveHi("stopped-greeting"); await lateGreeting;
  assert.equal(hi.played.length, 1);
  assert(hi.played[0].paused);

  // Turning wake off stops playback and an in-flight wake transcription.
  const off = runtime();
  let cancelled = 0, wakeStopped = 0;
  Object.assign(off.ctx, {
    wakeToggle: {checked: false}, fromWake: true,
    stopCloudWake() {wakeStopped++;},
    cancelRecording: async () => {cancelled++;},
  });
  const change = section("    wakeToggle.onchange = async () =>", "    startNewConversationDraft();");
  vm.runInContext(change, off.ctx);
  await off.ctx.wakeToggle.onchange();
  assert.equal(wakeStopped, 1); assert.equal(cancelled, 1);
  assert.equal(off.run("wakeActive"), false);
  assert.equal(off.run("speakNextReply"), false);
  assert.equal(off.run("replySpeechStopped"), true);
  assert.equal(off.ctx.fromWake, false);

  // Muting a continuous reply cannot restart on new deltas; task completion resumes listening.
  const continuous = runtime();
  vm.runInContext(`
    let continuousActive = true, continuousAwaitingReply = true, continuousTaskDone = true;
    let continuousReplyReady = true, continuousReplyText = "final", continuousToken = 2;
    ${section("    function beginStreamSpeech(", "    // ===== 说话输入(STT):")}
  `, continuous.ctx);
  let resumed = 0;
  continuous.ctx.resumeContinuousAfterSpeech = async () => {resumed++;};
  continuous.button.onclick();
  assert.equal(continuous.ctx.beginStreamSpeech(), null);
  assert.equal(continuous.run("continuousAwaitingReply"), false);
  assert.equal(resumed, 1);

  // ASR results from a cancelled or superseded recording cannot overwrite a newer draft.
  let finishAsr;
  const draft = {value: "new draft", focus() {}};
  const recognition = vm.createContext({
    audioCtx: {sampleRate: 16000, close: async () => {}},
    procNode: {disconnect() {}}, srcNode: {disconnect() {}}, mediaStream: {}, analyser: {},
    pcmChunks: [new Float32Array(160)], fromWake: true, recognitionSeq: 4,
    continuousActive: false, continuousToken: 0, voiceHint: {textContent: ""}, textEl: draft,
    parkMicStream() {}, setTimeout, clearTimeout,
    resampleForAsr: async samples => samples, floatTo16BitPCM: samples => samples,
    splitAsrPcm: samples => [samples],
    asrChunk: () => new Promise(resolve => {finishAsr = resolve;}),
    send() {throw Error("cancelled ASR must not send a message");},
  });
  vm.runInContext(section("    async function stopRecordingAndSend(", "    // 双模式:"), recognition);
  const transcribing = recognition.stopRecordingAndSend("auto"); await tick();
  recognition.recognitionSeq++;
  finishAsr({ok: true, text: "stale transcript"}); await transcribing;
  assert.equal(draft.value, "new draft");
  assert.equal(recognition.fromWake, true, "old finally cannot clear a newer wake turn");

  // PCM conversion uses browser resampling at the declared WAV sample rate.
  const audio = vm.createContext({window: {}, Float32Array, Int16Array, DataView, ArrayBuffer, Blob});
  vm.runInContext(section("    function floatTo16BitPCM(", "    function blobToBase64("), audio);
  const samples = new Float32Array([0, .5, -.5, 2, -2]);
  const same = await audio.resampleForAsr(samples, 16000, 16000);
  assert.equal(same, samples);
  const fallback = await audio.resampleForAsr(new Float32Array(48000), 48000, 16000);
  assert.equal(fallback.length, 16000);
  assert.equal((await audio.resampleForAsr(new Float32Array(8000), 8000, 16000)).length, 16000);
  let nativeCall;
  audio.window.OfflineAudioContext = class {
    constructor(channels, length, rate) { nativeCall = {channels, length, rate}; this.destination = {}; }
    createBuffer(channels, length, rate) { nativeCall.inputRate = rate; return {copyToChannel: input => { nativeCall.input = input; }}; }
    createBufferSource() { return {connect() {}, start() {}}; }
    createBiquadFilter() { return {frequency: {}, Q: {}, connect() {}}; }
    async startRendering() { return {getChannelData: () => new Float32Array(nativeCall.length)}; }
  };
  assert.equal((await audio.resampleForAsr(new Float32Array(44100), 44100, 16000)).length, 16000);
  assert.equal(nativeCall.inputRate, 44100);
  assert.equal(nativeCall.rate, 44100, "filtering must occur before downsampling");
  const wave = new DataView(await audio.encodeWav(audio.floatTo16BitPCM(samples), 16000).arrayBuffer());
  assert.equal(wave.getUint32(24, true), 16000);
  assert.equal(wave.getUint16(22, true), 1);
  assert.equal(wave.getInt16(50, true), 32767);
  assert.equal(wave.getInt16(52, true), -32768);
  const longPcm = new Int16Array(16000 * 120).fill(3000);
  longPcm.fill(0, 16000 * 52, 16000 * 53);
  const pieces = audio.splitAsrPcm(longPcm);
  assert(pieces.every(piece => piece.length <= 16000 * 55));
  assert.equal(pieces.reduce((sum, piece) => sum + piece.length, 0), longPcm.length);
  assert.equal(pieces[0].length, 16000 * 53, "long recordings split at the latest quiet pause");
  assert.equal(pieces[1].byteOffset, pieces[0].byteLength, "segments neither overlap nor drop samples");
  assert.equal(audio.splitAsrPcm(new Int16Array()).length, 0);
  console.log("voice controls: stop current/prefetched/pending audio, wake greeting, wake-off transcription, PCM resampling and silence-aware segmentation passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
