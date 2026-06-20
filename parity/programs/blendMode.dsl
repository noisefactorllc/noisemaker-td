search synth, mixer
noise(seed: 1, colorMode: 1).write(o0)
gradient(seed: 1).blendMode(tex: o0, mode: 13).write(o1)
render(o1)
