// NM_INPUTS: sourceTex=0
// NM_OUTPUT: fragColor
#define sourceTex sTD2DInputs[0]
// Copy Pass - Blit source to destination (for ping-pong correction after diffuse)
// This ensures the decayed trail is in the write buffer before deposit blends onto it



out vec4 fragColor;

void nm_main() {
    // Use actual texture size, not canvas resolution
    ivec2 texSize = textureSize(sourceTex, 0);
    vec2 uv = gl_FragCoord.xy / vec2(texSize);
    fragColor = texture(sourceTex, uv);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
