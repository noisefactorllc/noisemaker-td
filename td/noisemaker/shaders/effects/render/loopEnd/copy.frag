// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
/*
 * Simple copy/blit shader - copies input to output unchanged.
 * Used for feedback texture updates.
 */




out vec4 fragColor;

void nm_main() {
    ivec2 texSize = textureSize(inputTex, 0);
    vec2 uv = gl_FragCoord.xy / vec2(texSize);
    fragColor = texture(inputTex, uv);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
