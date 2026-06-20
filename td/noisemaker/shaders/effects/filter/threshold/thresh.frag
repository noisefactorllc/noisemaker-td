// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
uniform vec2 tileOffset;
uniform vec2 fullResolution;

uniform float level;
uniform float sharpness;

out vec4 fragColor;

/* Binary threshold with adjustable edge softness. */
void nm_main(){
  vec2 globalCoord = gl_FragCoord.xy + tileOffset;
  vec2 st = gl_FragCoord.xy / vec2(textureSize(inputTex,0));
  vec4 c = texture(inputTex, st);
  float l = dot(c.rgb, vec3(0.299,0.587,0.114));
  float e = smoothstep(level - sharpness, level + sharpness, l);
  fragColor = vec4(vec3(e),1.0);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
