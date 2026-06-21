search synth, mixer
noise(seed: 1, scaleX: 50, scaleY: 50).write(o0)
gradient(seed: 1).write(o1)
cell(seed: 1).write(o2)
channelCombine(rTex: o0, gTex: o1, bTex: o2).write(o3)
render(o3)
