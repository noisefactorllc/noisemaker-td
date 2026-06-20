// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
/*
 * Chromatic aberration effect.
 */



uniform vec2 resolution;
uniform vec2 tileOffset;
uniform vec2 fullResolution;
uniform float displacement;
out vec4 fragColor;

void nm_main() {
    vec2 globalPixel = gl_FragCoord.xy + tileOffset;
    vec2 globalUV = globalPixel / fullResolution;
    
    float maxDisplacementUV = 256.0 / fullResolution.x;
    float boundedDisplacement = clamp(displacement, -maxDisplacementUV, maxDisplacementUV);
    
    vec2 redGlobalUV = globalUV + vec2(boundedDisplacement, 0.0);
    vec2 redLocalUV = (redGlobalUV * fullResolution - tileOffset) / vec2(textureSize(inputTex, 0));
    redLocalUV.y = 1.0 - redLocalUV.y;
    vec4 red = texture(inputTex, redLocalUV);

    vec2 greenLocalUV = (globalUV * fullResolution - tileOffset) / vec2(textureSize(inputTex, 0));
    greenLocalUV.y = 1.0 - greenLocalUV.y;
    vec4 green = texture(inputTex, greenLocalUV);

    vec2 blueGlobalUV = globalUV - vec2(boundedDisplacement, 0.0);
    vec2 blueLocalUV = (blueGlobalUV * fullResolution - tileOffset) / vec2(textureSize(inputTex, 0));
    blueLocalUV.y = 1.0 - blueLocalUV.y;
    vec4 blue = texture(inputTex, blueLocalUV);

    fragColor = vec4(red.r, green.g, blue.b, green.a);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
