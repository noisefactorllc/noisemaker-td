// NM_INPUTS: (none)
// NM_OUTPUT: fragColor
// Clear pass - fill with background color (premultiplied alpha)

uniform vec3 bgColor;
uniform float bgAlpha;

out vec4 fragColor;

void nm_main() {
    fragColor = vec4(bgColor * bgAlpha, bgAlpha);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
