// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
uniform vec2 resolution;
uniform vec2 tileOffset;
uniform vec2 fullResolution;
uniform float aspect;
uniform float scaleX;
uniform float scaleY;
uniform float centerX;
uniform float centerY;
uniform int wrap;


out vec4 fragColor;

void nm_main(){
  // Compute global UV from tile-local coordinates
  vec2 globalCoord = gl_FragCoord.xy + tileOffset;
  vec2 st = globalCoord / fullResolution;
  
  // Apply scale transform in global UV space (centered and aspect-corrected)
  vec2 c = vec2(-centerX, centerY);
  st -= c;
  st.x *= aspect;
  st = st / vec2(scaleX, scaleY);
  st.x /= aspect;
  st += c;
  
  // Convert global UV to local UV for sampling inputTex
  vec2 localUV = (st * fullResolution - tileOffset) / resolution;
  
  // Apply wrap mode to local UV
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
