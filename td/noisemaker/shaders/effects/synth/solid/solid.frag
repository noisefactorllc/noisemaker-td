// NM_INPUTS: (none)
// NM_OUTPUT: fragColor
uniform vec3 color;
uniform float alpha;

out vec4 fragColor;

/* Produces a constant color with premultiplied alpha. */
void nm_main() {
  // Premultiply RGB by alpha for correct compositing
  fragColor = vec4(color * alpha, alpha);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
