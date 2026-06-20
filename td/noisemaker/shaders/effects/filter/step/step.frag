// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
/*
 * Step threshold effect
 * Creates hard edge at threshold value
 */


uniform vec2 tileOffset;
uniform vec2 fullResolution;

uniform float threshold;
uniform bool antialias;

out vec4 fragColor;

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    ivec2 texSize = textureSize(inputTex, 0);
    vec2 uv = gl_FragCoord.xy / vec2(texSize);
    vec4 color = texture(inputTex, uv);

    if (antialias) {
        vec3 fw = fwidth(color.rgb);
        color.rgb = smoothstep(threshold - fw * 0.5, threshold + fw * 0.5, color.rgb);
    } else {
        color.rgb = step(threshold, color.rgb);
    }

    fragColor = color;
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
