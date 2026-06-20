// NM_INPUTS: inputTex=0 feedbackTex=1
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
#define feedbackTex sTD2DInputs[1]
/*
 * Convolution Feedback - Blend Pass
 * Blends processed feedback texture with input based on intensity
 */




uniform float intensity;
uniform bool resetState;

out vec4 fragColor;

void nm_main() {
    ivec2 coord = ivec2(gl_FragCoord.xy);
    
    vec4 inputColor = texelFetch(inputTex, coord, 0);
    
    // If resetState is true, bypass feedback and return input directly
    if (resetState) {
        fragColor = inputColor;
        return;
    }
    
    vec4 feedback = texelFetch(feedbackTex, coord, 0);
    
    // Blend input with processed feedback based on intensity
    vec3 result = mix(inputColor.rgb, feedback.rgb, intensity);
    
    fragColor = vec4(result, inputColor.a);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
