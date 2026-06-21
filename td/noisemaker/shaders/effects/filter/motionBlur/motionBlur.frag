// NM_INPUTS: inputTex=0 selfTex=1
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
#define selfTex sTD2DInputs[1]
/*
 * Motion Blur - Simple frame blending shader.
 * Mixes current input with previous frame for a motion blur effect.
 * Amount 0-100 maps to mix factor (stronger at higher values).
 */




uniform vec2 resolution;
uniform float amount;
uniform bool resetState;

out vec4 fragColor;

void nm_main() {
    vec2 uv = gl_FragCoord.xy / resolution;
    
    // If resetState is true, bypass feedback and return input directly
    if (resetState) {
        fragColor = texture(inputTex, uv);
        return;
    }

    vec4 current = texture(inputTex, uv);
    vec4 previous = texture(selfTex, uv);
    
    // Map amount 0-100 to 0-0.8 (clamped)
    float mixFactor = clamp(amount * 0.008, 0.0, 0.98);
    
    fragColor = mix(current, previous, mixFactor);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
