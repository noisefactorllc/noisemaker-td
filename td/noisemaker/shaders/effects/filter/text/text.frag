// NM_INPUTS: inputTex=0 textTex=1
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
#define textTex sTD2DInputs[1]
/*
 * Text overlay shader
 * Blends pre-rendered text texture over input with matte background
 */




uniform vec2 resolution;
uniform vec2 tileOffset;
uniform vec2 fullResolution;
uniform vec3 matteColor;
uniform float matteOpacity;

out vec4 fragColor;

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    vec2 st = globalCoord / fullResolution;

    vec4 inputColor = texture(inputTex, gl_FragCoord.xy / vec2(textureSize(inputTex, 0)));
    vec4 text = texture(textTex, gl_FragCoord.xy / vec2(textureSize(textTex, 0)));

    // Text presence from canvas alpha
    float textPresence = text.a;
    float matteAlpha = matteOpacity;

    // Premultiplied blend (matches pointsRender):
    // - Text contribution (not affected by matte)
    // - Input passes through where no text AND no matte
    // - Matte replaces input where matteOpacity > 0
    vec3 rgb = text.rgb * textPresence
             + inputColor.rgb * (1.0 - textPresence) * (1.0 - matteAlpha)
             + matteColor * matteAlpha * (1.0 - textPresence);

    // Alpha: text=opaque, elsewhere blend input alpha toward opaque by matte
    float alpha = max(textPresence, mix(inputColor.a, 1.0, matteAlpha));

    fragColor = vec4(rgb, alpha);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
