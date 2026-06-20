// NM_INPUTS: sourceTex=0
// NM_OUTPUT: fragColor
#define sourceTex sTD2DInputs[0]
// Copy Pass - Blit source to destination (for ping-pong correction)


uniform vec2 resolution;

out vec4 fragColor;

void nm_main() {
    vec2 uv = gl_FragCoord.xy / resolution;
    fragColor = texture(sourceTex, uv);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
