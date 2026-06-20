// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
/*
 * Smoothstep threshold effect
 * Creates smooth transition between edge0 and edge1
 */


uniform vec2 tileOffset;
uniform vec2 fullResolution;

uniform float edge0;
uniform float edge1;

out vec4 fragColor;

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    ivec2 texSize = textureSize(inputTex, 0);
    vec2 uv = gl_FragCoord.xy / vec2(texSize);
    vec4 color = texture(inputTex, uv);

    color.rgb = smoothstep(edge0, edge1, color.rgb);

    fragColor = color;
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
