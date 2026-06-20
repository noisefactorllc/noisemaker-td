// NM_INPUTS: inputTex=0 gridTex=1
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
#define gridTex sTD2DInputs[1]
uniform vec2 resolution;
uniform float matteOpacity;

out vec4 fragColor;

void nm_main() {
    vec2 uv = gl_FragCoord.xy / resolution;
    vec4 inputCol = texture(inputTex, uv);
    vec4 grid = texture(gridTex, uv);
    
    // Blend grid structure over input
    // Grid alpha indicates structure presence
    float gridStrength = clamp(grid.a, 0.0, 1.0);
    vec3 gridColor = grid.rgb;
    float matteAlpha = matteOpacity;
    
    // Mix: where grid exists, show grid color; otherwise show input (premultiplied by matte)
    vec3 color = mix(inputCol.rgb * matteAlpha, gridColor, gridStrength);
    
    // Alpha: where grid exists, full opacity; elsewhere, matte opacity
    float alpha = max(gridStrength, matteAlpha);
    
    fragColor = vec4(color, alpha);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
