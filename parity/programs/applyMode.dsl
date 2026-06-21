search synth, mixer
noise(seed: 1, scaleX: 50, scaleY: 50).write(o0)
gradient(seed: 1).applyMode(tex: o0).write(o1)
render(o1)
