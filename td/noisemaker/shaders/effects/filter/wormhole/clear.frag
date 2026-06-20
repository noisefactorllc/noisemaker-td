// NM_INPUTS: (none)
// NM_OUTPUT: fragColor
out vec4 fragColor;

void nm_main() {
    fragColor = vec4(0.0);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
