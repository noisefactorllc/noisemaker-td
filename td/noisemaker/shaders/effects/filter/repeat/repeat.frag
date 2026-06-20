// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
uniform vec2 resolution;
uniform float aspect;
uniform float x;
uniform float y;
uniform float offsetX;
uniform float offsetY;
uniform int wrap;

uniform vec2 tileOffset;
uniform vec2 fullResolution;

out vec4 fragColor;

void nm_main(){
  // Compute global coordinate
  vec2 globalCoord = gl_FragCoord.xy + tileOffset;
  
  // Compute global UV
  vec2 globalUV = globalCoord / fullResolution;
  
  // Apply repeat transformation in global space
  vec2 st = globalUV;
  st.x *= aspect;
  st = st * vec2(x, y) + vec2(offsetX * aspect, offsetY);
  st.x /= aspect;
  
  // Apply wrap mode
  if (wrap == 0) {
      // mirror
      st = abs(mod(st + 1.0, 2.0) - 1.0);
  } else if (wrap == 1) {
      // repeat
      st = fract(st);
  } else {
      // clamp
      st = clamp(st, 0.0, 1.0);
  }
  
  // Convert warped global UV to local UV for sampling
  vec2 localUV = (st * fullResolution - tileOffset) / vec2(textureSize(inputTex, 0));
  
  // For seamless tiling across tile boundaries, apply wrap to local UV
  if (wrap == 0) {
      // mirror
      localUV = abs(mod(localUV + 1.0, 2.0) - 1.0);
  } else if (wrap == 1) {
      // repeat
      localUV = fract(localUV);
  } else {
      // clamp
      localUV = clamp(localUV, 0.0, 1.0);
  }
  
  fragColor = vec4(texture(inputTex, localUV).rgb, 1.0);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
