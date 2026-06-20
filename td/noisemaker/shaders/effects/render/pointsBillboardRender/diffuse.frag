// NM_INPUTS: trailTex=0
// NM_OUTPUT: fragColor
#define trailTex sTD2DInputs[0]
// Diffuse Pass - Decay existing trail


uniform vec2 resolution;
uniform float intensity;

out vec4 fragColor;

void nm_main() {
    vec2 uv = gl_FragCoord.xy / resolution;
    
    // Sample the trail texture directly (no blur)
    vec4 trailColor = texture(trailTex, uv);
    
    // Apply intensity decay (persistence)
    // intensity=100 means no decay, intensity=0 means instant fade
    float decay = clamp(intensity / 100.0, 0.0, 1.0);
    fragColor = trailColor * decay;
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
