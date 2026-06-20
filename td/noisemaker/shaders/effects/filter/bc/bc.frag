// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
/*
 * Brightness and contrast adjustment effect
 */


uniform vec2 tileOffset;
uniform vec2 fullResolution;

uniform float brightness;
uniform float contrast;

out vec4 fragColor;

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    ivec2 texSize = textureSize(inputTex, 0);
    vec2 uv = gl_FragCoord.xy / vec2(texSize);
    vec4 color = texture(inputTex, uv);

    // Apply brightness (multiply)
    color.rgb *= brightness;

    // Apply contrast (0..1 -> 0..2)
    float contrastFactor = contrast * 2.0;
    color.rgb = (color.rgb - 0.5) * contrastFactor + 0.5;

    fragColor = color;
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
