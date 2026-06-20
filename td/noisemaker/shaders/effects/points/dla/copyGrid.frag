// NM_INPUTS: gridTex=0
// NM_OUTPUT: fragColor
#define gridTex sTD2DInputs[0]
// Copy Pass - Blit grid to write buffer for proper blending


uniform vec2 resolution;

out vec4 fragColor;

void nm_main() {
    vec2 uv = gl_FragCoord.xy / resolution;
    fragColor = texture(gridTex, uv);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
