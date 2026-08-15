/**
 * Captures raw mono PCM for the live-caption relay (see useLiveCaption.ts).
 *
 * Runs on the audio rendering thread, not the main thread: it cannot touch
 * the DOM or React state, only read frames and post them out. Kept
 * deliberately dumb -- no downsampling, no Int16 conversion, no batching --
 * because this only ever sees one 128-sample render quantum per call, and
 * doing real work here risks under-running the audio thread. All of that
 * happens on the main thread instead, after these frames arrive over the
 * MessagePort.
 */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length > 0) {
      // .slice() copies out of the reused render-quantum buffer -- without
      // it, every posted message would end up pointing at whatever the next
      // callback overwrote it with.
      this.port.postMessage(channel.slice());
    }
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
