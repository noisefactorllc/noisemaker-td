// NM_INPUTS: srcTex=0
// NM_OUTPUT: fragColor
#define srcTex sTD2DInputs[0]
/*
 * Temporal Chromatic Aberration - shift pass (one stage of the delay line).
 *
 * Copies the source stage into the destination stage, advancing the bucket-brigade shift
 * register by one frame. Alpha is preserved unchanged so the "filled" frontier (alpha 1
 * from the live input vs. alpha 0 from never-written stages) propagates exactly one stage
 * per frame, which the read pass uses for its ramp-in fallback.
 */




out vec4 fragColor;

void nm_main() {
    ivec2 texSize = textureSize(srcTex, 0);
    vec2 uv = gl_FragCoord.xy / vec2(texSize);
    fragColor = texture(srcTex, uv);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
