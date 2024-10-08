# adapted from https://github.com/huggingface/diffusers/blob/v0.29.0/src/diffusers/models/embeddings.py
import torch

from diffusers.models.embeddings import PatchEmbed, get_2d_sincos_pos_embed
import torch.distributed
from xfuser.core.distributed.runtime_state import get_runtime_state
from xfuser.model_executor.layers import xFuserLayerBaseWrapper
from xfuser.model_executor.layers import xFuserLayerWrappersRegister
from xfuser.logger import init_logger

logger = init_logger(__name__)


@xFuserLayerWrappersRegister.register(PatchEmbed)
class xFuserPatchEmbedWrapper(xFuserLayerBaseWrapper):
    def __init__(
        self,
        patch_embedding: PatchEmbed,
    ):
        super().__init__(
            module=patch_embedding,
        )
        self.module: PatchEmbed
        self.pos_embed = None

    def forward(self, latent):
        height = (
            get_runtime_state().input_config.height
            // get_runtime_state().vae_scale_factor
        )
        width = latent.shape[-1]
        if not get_runtime_state().patch_mode:
            if getattr(self.module, "pos_embed_max_size", None) is not None:
                pass
            else:
                height, width = (
                    height // self.module.patch_size,
                    width // self.module.patch_size,
                )
        else:
            if getattr(self.module, "pos_embed_max_size", None) is not None:
                pass
            else:
                height, width = (
                    height // self.module.patch_size,
                    width // self.module.patch_size,
                )

        latent = self.module.proj(latent)
        if self.module.flatten:
            latent = latent.flatten(2).transpose(1, 2)  # BCHW -> BNC
        if self.module.layer_norm:
            # TODO: NOT SURE whether compatible with norm
            latent = self.module.norm(latent)

        # [2, 4096 / c, 1152]

        if self.module.pos_embed is None:
            return latent.to(latent.dtype)

        # Interpolate positional embeddings if needed.
        # (For PixArt-Alpha: https://github.com/PixArt-alpha/PixArt-alpha/blob/0f55e922376d8b797edd44d25d0e7464b260dcab/diffusion/model/nets/PixArtMS.py#L162C151-L162C160)

        # TODO: There might be a more faster way to generate a smaller pos_embed
        if getattr(self.module, "pos_embed_max_size", None):
            pos_embed = self.module.cropped_pos_embed(height, width)
        else:
            if self.module.height != height or self.module.width != width:
                pos_embed = get_2d_sincos_pos_embed(
                    embed_dim=self.module.pos_embed.shape[-1],
                    grid_size=(height, width),
                    base_size=self.module.base_size,
                    interpolation_scale=self.module.interpolation_scale,
                )
                pos_embed = torch.from_numpy(pos_embed)
                self.module.pos_embed = pos_embed.float().unsqueeze(0).to(latent.device)
                self.module.height = height
                self.module.width = width
                pos_embed = self.module.pos_embed
            else:
                pos_embed = self.module.pos_embed
        b, c, h = pos_embed.shape

        if get_runtime_state().patch_mode:
            start, end = get_runtime_state().pp_patches_token_start_end_idx_global[
                get_runtime_state().pipeline_patch_idx
            ]
            pos_embed = pos_embed[
                :,
                start:end,
                :,
            ]
        else:
            pos_embed_list = [
                pos_embed[
                    :,
                    get_runtime_state()
                    .pp_patches_token_start_end_idx_global[i][0] : get_runtime_state()
                    .pp_patches_token_start_end_idx_global[i][1],
                    :,
                ]
                for i in range(get_runtime_state().num_pipeline_patch)
            ]
            pos_embed = torch.cat(pos_embed_list, dim=1)

        return (latent + pos_embed).to(latent.dtype)
