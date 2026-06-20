// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
/*
 * Invert brightness effect
 * Simple RGB inversion: 1.0 - value
 */


uniform vec2 tileOffset;
uniform vec2 fullResolution;


out vec4 fragColor;

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    ivec2 texSize = textureSize(inputTex, 0);
    vec2 uv = gl_FragCoord.xy / vec2(texSize);
    vec4 color = texture(inputTex, uv);

    color.rgb = 1.0 - color.rgb;

    fragColor = color;
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
