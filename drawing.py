import secrets
from dataclasses import dataclass

SIZES = {"竖图": (832, 1216), "横图": (1216, 832)}
DIRECTIONS = {"横": "横图", "横图": "横图", "横向": "横图", "竖": "竖图", "竖图": "竖图", "竖向": "竖图"}
@dataclass
class Drawing:
    direction: str
    scene: str
    negative: str
    characters: list

def build_request(drawing, original_request=None):
    seed = secrets.randbelow(2**32)
    if original_request is not None:
        old_seed = original_request["parameters"].get("seed")
        if seed == old_seed:
            seed = (seed + 1) % 2**32
        params = {**original_request["parameters"], "seed": seed}
        # v1.12.0–1 saved the UI's English name instead of the V5 wire value.
        descriptions = params.get("director_reference_descriptions")
        if descriptions:
            descriptions = list(descriptions)
            for index, item in enumerate(descriptions):
                cap = item["caption"]
                if cap.get("base_caption") == "outfit":
                    descriptions[index] = {**item, "caption": {**cap, "base_caption": "costume"}}
            params["director_reference_descriptions"] = descriptions
        return {**original_request, "parameters": params}
    def caption(base, side):
        return {"base_caption": base, "char_captions": [
            {"char_caption": pair[side], "centers": [{"x": 0.5, "y": 0.5}]}
            for pair in drawing.characters
        ]}
    width, height = SIZES[drawing.direction]
    return {
        "input": drawing.scene, "model": "nai-diffusion-5-full", "action": "generate",
        "parameters": {
            "prompt": drawing.scene, "params_version": 4, "width": width, "height": height, "steps": 28,
            "n_samples": 1, "seed": seed, "scale": 7,
            "sampler": "k_euler_ancestral", "noise_schedule": "karras", "cfg_rescale": 0,
            "use_coords": False, "qualityToggle": False, "prefer_brownian": True,
            "negative_prompt": drawing.negative,
            "v4_prompt": {"caption": caption(drawing.scene, 0), "use_coords": False, "use_order": True},
            "v4_negative_prompt": {"caption": caption(drawing.negative, 1), "legacy_uc": False},
        },
    }
