search synth, filter, render, points, mixer

perlin(
  scale: 100,
  octaves: 2,
  dimensions: 3,
  seed: 48
)
  .subchain(name: "flow field particles", id: "lkjw") {
    .pointsEmit(stateSize: x1024)
    .flow(
      behavior: chaotic,
      stride: 51,
      strideDeviation: 0.5,
      kink: 5.4
    )
    .pointsRender(
      density: 100,
      intensity: 74.59,
      inputIntensity: 21.46
    )
    .pointsBillboardRender(
      shapeMode: soft,
      depositOpacity: 100,
      pointSize: 33.19,
      sizeVariation: 100,
      seed: 0,
      density: 0.78,
      intensity: 44.72,
      inputIntensity: 18.23
    )
  }
  .blur()
  .write(o0)

navierStokes(
  tex: read(o0),
  zoom: x4,
  iterations: 40,
  smoothing: bSpline4x4,
  speed: 145,
  dyeDecay: 97.52,
  velocityDecay: 100,
  inputForce: 1,
  inputDye: 1,
  inputIntensity: 6.01
)
  .palette(
    index: solaris,
    offset: 7,
    alpha: 0.67
  )
  .lighting(
    normalStrength: 5,
    smoothing: 1.8,
    specularIntensity: 0.7,
    shininess: 130,
    reflection: 21.8,
    refraction: 23,
    aberration: 18.4
  )
  .adjust(brightness: 1.9, contrast: 0.8)
  .subchain(name: "lens effects", id: "dpxp") {
    .temporalAberration(
      redDelay: 4.7,
      greenDelay: 1.8,
      blueDelay: 0,
      _skip: true
    )
    .bloom(taps: 15)
    .lens(displacement: -0.28)
    .vignette(brightness: 0.36, alpha: 0.45)
  }
  .grain(alpha: 0.18, _skip: true)
  .write(o1)

render(o1)
