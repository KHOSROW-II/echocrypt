class EchoCryptPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frameSamples = 960; // 20 ms at 48 kHz
    this.pending = new Float32Array(this.frameSamples);
    this.offset = 0;
  }

  process(inputs, outputs) {
    const inputChannels = inputs[0] || [];
    const outputChannels = outputs[0] || [];

    // Keep the captured tab audible through the user's normal output device.
    for (let channel = 0; channel < outputChannels.length; channel += 1) {
      const source = inputChannels[Math.min(channel, inputChannels.length - 1)];
      if (source) {
        outputChannels[channel].set(source);
      } else {
        outputChannels[channel].fill(0);
      }
    }

    if (inputChannels.length === 0) {
      return true;
    }

    const frameCount = inputChannels[0].length;
    for (let frame = 0; frame < frameCount; frame += 1) {
      let mono = 0;
      for (let channel = 0; channel < inputChannels.length; channel += 1) {
        mono += inputChannels[channel][frame] || 0;
      }
      mono /= inputChannels.length;
      this.pending[this.offset] = Math.max(-1, Math.min(1, mono));
      this.offset += 1;

      if (this.offset === this.frameSamples) {
        const pcm = new Int16Array(this.frameSamples);
        for (let index = 0; index < this.frameSamples; index += 1) {
          const value = this.pending[index];
          pcm[index] = value < 0
            ? Math.round(value * 32768)
            : Math.round(value * 32767);
        }
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
        this.pending = new Float32Array(this.frameSamples);
        this.offset = 0;
      }
    }

    return true;
  }
}

registerProcessor("echocrypt-pcm", EchoCryptPcmProcessor);
