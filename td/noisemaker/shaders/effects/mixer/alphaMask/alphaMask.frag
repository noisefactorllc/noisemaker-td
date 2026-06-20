// NM_INPUTS: inputTex=0 tex=1
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
#define tex sTD2DInputs[1]
uniform vec2 resolution;
uniform vec2 tileOffset;
uniform vec2 fullResolution;
uniform float mixAmt;
uniform bool maskMode;
out vec4 fragColor;

float map(float value, float inMin, float inMax, float outMin, float outMax) {
    return outMin + (outMax - outMin) * (value - inMin) / (inMax - inMin);
}

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    vec2 st = globalCoord / fullResolution;

    vec4 color1 = texture(inputTex, gl_FragCoord.xy / vec2(textureSize(inputTex, 0)));
    vec4 color2 = texture(tex, gl_FragCoord.xy / vec2(textureSize(tex, 0)));

    // luminance mask mode
    if (maskMode) {
        float maskVal = dot(color2.rgb, vec3(0.299, 0.587, 0.114));
        fragColor = vec4(color1.rgb, color1.a * maskVal);
        return;
    }

    // alpha blend. slider direction selects which input is on top, so either slot
    // can serve as the alpha source — slide negative for A-on-top, positive for
    // B-on-top. each half reaches a full Porter-Duff source-over at the midpoint.
    vec4 color;
    if (mixAmt < 0.0) {
        vec4 AoverB = color2 * (1.0 - color1.a) + color1 * color1.a;
        color = mix(color1, AoverB, map(mixAmt, -100.0, 0.0, 0.0, 1.0));
    } else {
        vec4 BoverA = color1 * (1.0 - color2.a) + color2 * color2.a;
        color = mix(BoverA, color2, map(mixAmt, 0.0, 100.0, 0.0, 1.0));
    }

    color.a = max(color1.a, color2.a);
    fragColor = color;
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
