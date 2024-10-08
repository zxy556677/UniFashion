"""
 Copyright (c) 2023, salesforce.com, inc.
 All rights reserved.
 SPDX-License-Identifier: BSD-3-Clause
 For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
"""
import logging

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.cuda.amp import autocast as autocast
from torch.nn import functional as F

from lavis.common.registry import registry
from lavis.models.base_model import all_gather_with_grad, concat_all_gather
from lavis.models.blip2_models.blip2 import (
    Blip2Base,
    compute_sim_matrix,
    disabled_train,
)
from lavis.models.blip_models.blip_outputs import BlipOutput, BlipOutputFeatures

from rq_vae_transformer.rqvae.models.rqvae.quantizations import RQBottleneck
from rq_vae_transformer.rqvae.models.rqvae.modules import Encoder, Decoder
from einops import repeat

@registry.register_model("blip2_cir_rerank")
class Blip2QformerCirRerank(Blip2Base):
    """
    BLIP2 first-stage model with Q-former and ViT.
    Supported model types:
        - pretrained: pretrained model with vit-g
        - pretrain_vitL: pretrained model with vit-large
        - coco: fintuned model on coco
    Usage:
        >>> from lavis.models import load_model
        >>> model = load_model("blip2", "pretrain")
    """

    PRETRAINED_MODEL_CONFIG_DICT = {
        "pretrain": "configs/models/blip2/blip2_pretrain.yaml",
        "pretrain_vitL": "configs/models/blip2/blip2_pretrain_vitL.yaml",
    }

    def __init__(
            self,
            vit_model="eva_clip_g",
            img_size=224,
            drop_path_rate=0,
            use_grad_checkpoint=False,
            vit_precision="fp16",
            freeze_vit=True,
            num_query_token=32,
            cross_attention_freq=2,
            embed_dim=256,
            max_txt_len=64,
    ):
        super().__init__()

        self.tokenizer = self.init_tokenizer()

        self.visual_encoder, self.ln_vision = self.init_vision_encoder(
            vit_model, img_size, drop_path_rate, use_grad_checkpoint, vit_precision
        )
        if freeze_vit:
            for name, param in self.visual_encoder.named_parameters():
                param.requires_grad = False
            self.visual_encoder = self.visual_encoder.eval()
            self.visual_encoder.train = disabled_train
            logging.info("freeze vision encoder")
        self.Qformer, self.query_tokens = self.init_Qformer(
            num_query_token, self.visual_encoder.num_features, cross_attention_freq
        )

        self.Qformer.resize_token_embeddings(len(self.tokenizer))

        state_dict = self.Qformer.state_dict()
        for name, param in self.Qformer.named_parameters():
            if "_query" in name:
                key_orig = name.replace("_query", "")
                param.data.copy_(state_dict[key_orig])

        self.vision_proj = nn.Linear(self.Qformer.config.hidden_size, embed_dim)
        self.text_proj = nn.Linear(self.Qformer.config.hidden_size, embed_dim)
        self.caption_proj = nn.Linear(self.Qformer.config.hidden_size, embed_dim)
        self.query_proj = nn.Linear(embed_dim, embed_dim)

        self.itm_head = nn.Linear(self.Qformer.config.hidden_size, 2)

        self.temp = nn.Parameter(0.07 * torch.ones([]))

        self.max_txt_len = max_txt_len

        self.prompt_tokens = nn.Parameter(
            torch.zeros(1, num_query_token, self.Qformer.config.hidden_size)
        )
        self.prompt_tokens.data.normal_(mean=0.0, std=self.Qformer.config.initializer_range)

    def forward(self, samples):
        image = samples["image"]
        target = samples["target"]
        text = samples["text_input"]
        reference_caption = samples["reference_caption"]
        target_caption = samples["target_caption"]

        ###============== reference text fusion ===================###
        # reference image feature
        image_embeds = self.ln_vision(self.visual_encoder(image))
        image_atts = torch.ones(image_embeds.size()[:-1], dtype=torch.long).to(
            image.device
        )
        # query tokens
        query_tokens = self.query_tokens.expand(image_embeds.shape[0], -1, -1)
        query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(
            self.device
        )
        prompt_tokens = self.prompt_tokens.expand(image_embeds.shape[0], -1, -1)
        prompt_atts = torch.ones(prompt_tokens.size()[:-1], dtype=torch.long).to(
            self.device
        )
        # text tokens
        self.tokenizer.truncation_side = 'right'

        text_tokens = self.tokenizer(
            text,
            padding="max_length",
            truncation=True,
            max_length=self.max_txt_len,
            return_tensors="pt",
        ).to(image.device)

        reference_tokens = self.tokenizer(
            reference_caption,
            padding="max_length",
            truncation=True,
            max_length=self.max_txt_len,
            return_tensors="pt",
        ).to(image.device)

        target_tokens = self.tokenizer(
            target_caption,
            padding="max_length",
            truncation=True,
            max_length=self.max_txt_len,
            return_tensors="pt",
        ).to(image.device)

        attention_mask = torch.cat([query_atts, text_tokens.attention_mask], dim=1)
        fusion_output = self.Qformer.bert(
            text_tokens.input_ids,
            query_embeds=query_tokens,
            attention_mask=attention_mask,
            encoder_hidden_states=image_embeds,
            encoder_attention_mask=image_atts,
            return_dict=True,
        )

        fusion_feats = F.normalize(
            self.text_proj(fusion_output.last_hidden_state[:, 32, :]), dim=-1
        )

        image_feats = F.normalize(
            self.vision_proj(fusion_output.last_hidden_state[:, : query_tokens.size(1), :]), dim=-1
        )

        target_embeds = self.ln_vision(self.visual_encoder(target))
        target_atts = torch.ones(target_embeds.size()[:-1], dtype=torch.long).to(
            image.device
        )
        target_output = self.Qformer.bert(
            query_embeds=prompt_tokens,
            encoder_hidden_states=target_embeds,
            encoder_attention_mask=target_atts,
            use_cache=True,
            return_dict=True,
        )

        target_feats = F.normalize(
            self.vision_proj(target_output.last_hidden_state), dim=-1
        )

        ###============== Fusion-target Contrastive ===================###
        # reference image feature

        sim_t2q = torch.matmul(
            fusion_feats.unsqueeze(1).unsqueeze(1), target_feats.permute(0, 2, 1)
        ).squeeze()

        sim_q2t = torch.matmul(
            target_feats.unsqueeze(1), fusion_feats.unsqueeze(-1)
        ).squeeze()

        bs = image.size(0)
        sim_t2i, _ = sim_t2q.max(-1)
        sim_i2t, _ = sim_q2t.max(-1)

        sim_t2i = sim_t2i / self.temp
        sim_i2t = sim_i2t / self.temp

        targets = torch.linspace(0, bs - 1, bs, dtype=int).to(
            image.device
        )

        loss_itc = F.cross_entropy(sim_i2t, targets) + F.cross_entropy(sim_t2i, targets)

        ###============== Re-Ranking based on caption ===================###

        text_target_only_output = self.Qformer.bert(
            target_tokens.input_ids,
            attention_mask=target_tokens.attention_mask,
            return_dict=True,
        )

        text_target_target_only_feat = F.normalize(
            self.text_proj(text_target_only_output.last_hidden_state[:, 0, :]), dim=-1
        )

        sim_t2t = torch.matmul(
            image_feats.unsqueeze(1), text_target_target_only_feat.unsqueeze(-1)
        ).squeeze()

        sim_t2t, _ = sim_t2t.max(-1)
        sim_t2t = sim_t2t / self.temp

        loss_ttc = F.cross_entropy(sim_t2t, targets)

        ###============== Re-Ranking based on similarity ===================###
        # hard negtives
        with torch.no_grad():
            # sim_copy = sim_i2t.clone().detach()
            sim_i2t.fill_diagonal_(-10000)
            sim_t2i.fill_diagonal_(-10000)
            weights_i2t = F.softmax(sim_i2t, dim=1)
            weights_t2i = F.softmax(sim_t2i, dim=1)

        # select a negative text for each image
        text_ids_neg = []
        text_atts_neg = []
        mark_same = False
        for b in range(bs):
            neg_idx = torch.argmax(weights_t2i[b], dim=-1).item()
            # if text_tokens.input_ids[b].equal(text_tokens.input_ids[neg_idx]):
            #     mark_same = True
            #     neg_idx = torch.multinomial(weights_t2i[b], 1).item()
            text_ids_neg.append(text_tokens.input_ids[neg_idx])
            text_atts_neg.append(text_tokens.attention_mask[neg_idx])

        text_ids_neg = torch.stack(text_ids_neg, dim=0)
        text_atts_neg = torch.stack(text_atts_neg, dim=0)

        # select a negative target image for each text
        target_embeds_neg = []
        for b in range(bs):
            neg_idx = torch.argmax(weights_i2t[b], dim=-1).item()
            # if mark_same:
            #     neg_idx = torch.multinomial(weights_i2t[b], 1).item()
            target_embeds_neg.append(target_embeds[neg_idx])

        image_embeds_neg = torch.stack(target_embeds_neg, dim=0)

        text_ids_all = torch.cat(
            [target_tokens.input_ids, target_tokens.input_ids, text_ids_neg], dim=0
        )  # pos, pos, neg

        text_atts_all = torch.cat(
            [target_tokens.attention_mask, target_tokens.attention_mask, text_atts_neg],
            dim=0,
        )

        # query_tokens_itm = fusion_output.last_hidden_state[:, : query_tokens.size(1), :]

        target_embeds_all = torch.cat([target_embeds, image_embeds_neg, target_embeds], dim=0)

        target_atts_all = torch.ones(target_embeds_all.size()[:-1], dtype=torch.long).to(
            image.device
        )

        query_tokens_itm = self.prompt_tokens.expand(text_ids_all.shape[0], -1, -1)

        query_atts_itm = torch.ones(query_tokens_itm.size()[:-1], dtype=torch.long).to(
            image.device
        )

        attention_mask_all = torch.cat([query_atts_itm, text_atts_all], dim=1)

        output_itm = self.Qformer.bert(
            text_ids_all,
            query_embeds=query_tokens_itm,
            attention_mask=attention_mask_all,
            encoder_hidden_states=target_embeds_all,
            encoder_attention_mask=target_atts_all,
            return_dict=True,
        )

        vl_embeddings = output_itm.last_hidden_state[:, : 32, :]
        vl_output = self.itm_head(vl_embeddings)
        logits = vl_output.mean(dim=1)
        itm_labels = torch.cat(
            [torch.ones(bs, dtype=torch.long), torch.zeros(2 * bs, dtype=torch.long)],
            dim=0,
        ).to(image.device)
        loss_itm = F.cross_entropy(logits, itm_labels)

        ##================= Image captioning ========================##
        need_lm = False
        if need_lm:
            decoder_input_ids = text_tokens.input_ids.clone()
            decoder_input_ids[:, 0] = self.tokenizer.bos_token_id
            # print(decoder_input_ids)
            labels = decoder_input_ids.masked_fill(
                decoder_input_ids == self.tokenizer.pad_token_id, -100
            )
            # print(labels)
            query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(
                image.device
            )
            # attention_mask_code = torch.cat([query_atts, query_atts, code_tokens.attention_mask], dim=1)
            attention_mask = torch.cat([query_atts, text_tokens.attention_mask], dim=1)

            lm_output = self.Qformer(
                decoder_input_ids,
                attention_mask=attention_mask,
                past_key_values=query_output.past_key_values,
                return_dict=True,
                labels=labels,
            )

            loss_lm = lm_output.loss

        return {
            'loss_itc': loss_itc,
            'loss_itm': loss_itm,
            # 'loss_lm': loss_lm,
            'loss_ttc': loss_ttc
        }

    @torch.no_grad()
    def generate(
            self,
            samples,
            use_nucleus_sampling=False,
            num_beams=3,
            max_length=30,
            min_length=10,
            top_p=0.9,
            repetition_penalty=1.0,
    ):
        """
        Args:
            samples (dict): A dictionary containing the following keys:
                - image (torch.Tensor): A tensor of shape (batch_size, 3, H, W)
            use_nucleus_sampling (bool): Whether to use nucleus sampling. If False, use top-k sampling.
            num_beams (int): Number of beams for beam search. 1 means no beam search.
            max_length (int): The maximum length of the sequence to be generated.
            min_length (int): The minimum length of the sequence to be generated.
            top_p (float): The cumulative probability for nucleus sampling.
            repetition_penalty (float): The parameter for repetition penalty. 1.0 means no penalty.
            num_captions (int): Number of captions to be generated for each image.
        Returns:
            captions (list): A list of strings of length batch_size * num_captions.
        """
        image = samples["image"]
        image_embeds = self.ln_vision(self.visual_encoder(image))

        if not use_nucleus_sampling:
            image_embeds = image_embeds.repeat_interleave(num_beams, dim=0)
        else:
            num_beams = 1
        image_atts = torch.ones(image_embeds.size()[:-1], dtype=torch.long).to(
            image.device
        )

        model_kwargs = {
            "encoder_hidden_states": image_embeds,
            "encoder_attention_mask": image_atts,
        }

        input_ids = (
            torch.LongTensor(image.size(0), 1)
            .fill_(self.tokenizer.bos_token_id)
            .to(image.device)
        )
        query_tokens = self.query_tokens.expand(image_embeds.shape[0], -1, -1)

        outputs = self.Qformer.generate(
            input_ids=input_ids,
            query_embeds=query_tokens,
            max_length=max_length,
            min_length=min_length,
            num_beams=num_beams,
            do_sample=use_nucleus_sampling,
            top_p=top_p,
            eos_token_id=self.tokenizer.sep_token_id,
            pad_token_id=self.tokenizer.pad_token_id,
            **model_kwargs
        )
        captions = self.tokenizer.batch_decode(outputs, skip_special_tokens=True)
        return captions

    def forward_image(self, image):
        image_embeds = self.ln_vision(self.visual_encoder(image))
        image_atts = torch.ones(image_embeds.size()[:-1], dtype=torch.long).to(
            image.device
        )

        query_tokens = self.query_tokens.expand(image_embeds.shape[0], -1, -1)

        query_output = self.Qformer.bert(
            query_embeds=query_tokens,
            encoder_hidden_states=image_embeds,
            encoder_attention_mask=image_atts,
            return_dict=True,
        )
        return query_output.last_hidden_state, image_embeds

    def forward_text(self, text_tokens):
        text_output = self.Qformer.bert(
            text_tokens.input_ids,
            attention_mask=text_tokens.attention_mask,
            return_dict=True,
        )
        return text_output.last_hidden_state[:, 0, :]

    def compute_itm(self, image_inputs, text):

        text_tokens = self.tokenizer(
            text,
            padding="max_length",
            truncation=True,
            max_length=48,
            return_tensors="pt",
        ).to(image_inputs.device)
        target_size = 100
        # refereence_embeds = repeat(refereence_embeds, 'b l d -> (b t) l d', t=target_size)
        input_ids = repeat(text_tokens.input_ids, 'b l -> (b t) l', t=target_size)
        text_atts = repeat(text_tokens.attention_mask, 'b l -> (b t) l', t=target_size)

        image_atts = torch.ones(image_inputs.size()[:-1], dtype=torch.long).to(
            image_inputs.device
        )
        query_tokens = self.query_tokens.expand(image_inputs.shape[0], -1, -1)
        query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(
            image_inputs.device
        )

        attention_mask = torch.cat([query_atts, text_atts], dim=1)
        output_itm = self.Qformer.bert(
            input_ids,
            query_embeds=query_tokens,
            attention_mask=attention_mask,
            encoder_hidden_states=image_inputs,
            encoder_attention_mask=image_atts,
            return_dict=True,
        )
        vl_embeddings = output_itm.last_hidden_state[:, : query_tokens.size(1), :]
        itm_logit = self.itm_head(vl_embeddings)
        itm_logit = itm_logit[:, :, 1].mean(dim=1)
        return itm_logit

    @torch.no_grad()
    def inference(self, reference_embeds, target_feats, text, target_captions=None):
        image_atts = torch.ones(reference_embeds.size()[:-1], dtype=torch.long).to(
            reference_embeds.device
        )
        # query tokens
        query_tokens = self.query_tokens.expand(reference_embeds.shape[0], -1, -1)
        query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(
            self.device
        )
        # text tokens
        text_tokens = self.tokenizer(
            text,
            padding="max_length",
            truncation=True,
            max_length=self.max_txt_len,
            return_tensors="pt",
        ).to(reference_embeds.device)

        attention_mask = torch.cat([query_atts, text_tokens.attention_mask], dim=1)
        fusion_output = self.Qformer.bert(
            text_tokens.input_ids,
            query_embeds=query_tokens,
            attention_mask=attention_mask,
            encoder_hidden_states=reference_embeds,
            encoder_attention_mask=image_atts,
            return_dict=True,
        )

        fusion_feats = F.normalize(
            self.text_proj(fusion_output.last_hidden_state[:, 32, :]), dim=-1
        )

        sim_t2q = torch.matmul(
            fusion_feats.unsqueeze(1).unsqueeze(1), target_feats.permute(0, 2, 1)
        ).squeeze()

        sim_t2q, _ = sim_t2q.max(-1)

        if target_captions is not None:
            sim_t2t = torch.matmul(
                fusion_feats, target_captions.t()
            )
            sim_all = sim_t2q+sim_t2t
            return sim_all, sim_t2q

        return sim_t2q, sim_t2q

    def inference_rerank(self, refereence_embeds, target_embeds, text):
        """
        refereence_embeds: 16 * 257 * 768
        target_embeds: 100 * 257 * 768
        """
        target_size = target_embeds.size(0)
        ref_size = refereence_embeds.size(0)
        if ref_size > 1:
            target_size = int(target_size // ref_size)
        refereence_embeds = repeat(refereence_embeds, 'b l d -> (b t) l d', t=target_size)
        # target_embeds = repeat(target_embeds, 'b l d -> (r b) l d', r = ref_size)

        text_tokens = self.tokenizer(
            text,
            padding="max_length",
            truncation=True,
            max_length=self.max_txt_len,
            return_tensors="pt",
        ).to(refereence_embeds.device)
        text_inputs = repeat(text_tokens.input_ids, 'b l -> (b t) l', t=target_size)
        text_atts = repeat(text_tokens.attention_mask, 'b l -> (b t) l', t=target_size)

        query_tokens_itm = self.query_tokens.expand(text_inputs.shape[0], -1, -1)

        query_atts_itm = torch.ones(query_tokens_itm.size()[:-1], dtype=torch.long).to(
            refereence_embeds.device
        )
        attention_mask_all = torch.cat([query_atts_itm, text_atts], dim=1)

        target_atts_all = torch.ones(target_embeds.size()[:-1], dtype=torch.long).to(
            target_embeds.device
        )

        output_itm = self.Qformer.bert(
            text_inputs,
            query_embeds=query_tokens_itm,
            attention_mask=attention_mask_all,
            encoder_hidden_states=torch.cat([refereence_embeds, target_embeds], dim=1),
            encoder_attention_mask=torch.cat([target_atts_all, target_atts_all], dim=1),
            return_dict=True,
        )
        vl_embeddings = output_itm.last_hidden_state[:, : 32, :]
        vl_output = self.itm_head(vl_embeddings)
        logits = vl_output.mean(dim=1)
        logits = F.softmax(logits, dim=-1)

        return logits[:, -1]

    @torch.no_grad()
    def extract_target_features(self, image, mode='mean'):
        with self.maybe_autocast():
            image_embeds_frozen = self.ln_vision(self.visual_encoder(image))
        image_embeds_frozen = image_embeds_frozen.float()
        image_atts = torch.ones(
            image_embeds_frozen.size()[:-1], dtype=torch.long
        ).to(self.device)
        query_tokens = self.query_tokens.expand(
            image_embeds_frozen.shape[0], -1, -1
        )

        query_output = self.Qformer.bert(
            query_embeds=query_tokens,
            encoder_hidden_states=image_embeds_frozen,
            encoder_attention_mask=image_atts,
            return_dict=True,
        )
        image_embeds = query_output.last_hidden_state

        # return image_embeds
        image_features = F.normalize(self.vision_proj(image_embeds), dim=-1)
        return image_features, image_embeds_frozen

    @torch.no_grad()
    def extract_caption_features(self, caption, mode='mean'):

        caption_tokens = self.tokenizer(
            caption,
            padding="max_length",
            truncation=True,
            max_length=self.max_txt_len,
            return_tensors="pt",
        ).to(self.device)

        text_target_only_output = self.Qformer.bert(
            caption_tokens.input_ids,
            attention_mask=caption_tokens.attention_mask,
            return_dict=True,
        )

        text_target_target_only_feat = F.normalize(
            self.text_proj(text_target_only_output.last_hidden_state[:, 0, :]), dim=-1
        )

        return text_target_target_only_feat

    @torch.no_grad()
    def extract_features(self, samples, mode="multimodal"):
        """
        Extract features for multimodal or unimodal samples.
        Args:
            samples (dict): A dictionary of samples, containing the following keys:
                - image (torch.Tensor): A tensor of shape (B, C, H, W) containing the image.
                    Raw images should be preprocessed before being passed to feature extractor.
                - text_input (list): A list of strings containing the text, length B.
            mode (str): The mode of feature extraction. Can be either "multimodal", "text" or "image".
                If "multimodal", return image features and multimodal features;
                if "text", return text features;
                if "image", return image features.
                Default: "multimodal".
        Returns:
            BlipOutputFeatures: A BlipOutputFeatures object containing the features.
                See lavis/models/blip_models/blip_outputs.py for more details.
        """
        image = samples.get("image")
        caption = samples.get("text_input")

        # assert mode is one of "image", "text", "multimodal"
        assert mode in [
            "image",
            "text",
            "multimodal",
        ], "mode must be one of 'image', 'text', 'multimodal'"

        # initalize output
        image_embeds, text_embeds, multimodal_embeds = None, None, None
        image_features, text_features = None, None

        if mode == "image":
            assert (
                    image is not None
            ), "Image is not provided for mode 'image' or 'multimodal'"
            # return query features
            with self.maybe_autocast():
                image_embeds_frozen = self.ln_vision(self.visual_encoder(image))
            image_embeds_frozen = image_embeds_frozen.float()
            image_atts = torch.ones(
                image_embeds_frozen.size()[:-1], dtype=torch.long
            ).to(self.device)
            query_tokens = self.query_tokens.expand(
                image_embeds_frozen.shape[0], -1, -1
            )

            query_output = self.Qformer.bert(
                query_embeds=query_tokens,
                encoder_hidden_states=image_embeds_frozen,
                encoder_attention_mask=image_atts,
                return_dict=True,
            )
            image_embeds = query_output.last_hidden_state
            image_features = F.normalize(self.vision_proj(image_embeds), dim=-1)

        elif mode == "text":
            assert (
                    caption is not None
            ), "text input is None for mode 'text' or 'multimodal'"

            # return text features
            text = self.tokenizer(caption, return_tensors="pt", padding=True).to(
                self.device
            )

            text_output = self.Qformer.bert(
                text.input_ids,
                attention_mask=text.attention_mask,
                return_dict=True,
            )
            text_embeds = text_output.last_hidden_state
            text_features = self.text_proj(text_embeds)
            text_features = F.normalize(text_features, dim=-1)

        elif mode == "multimodal":
            # return multimodel query features
            with self.maybe_autocast():
                image_embeds_frozen = self.ln_vision(self.visual_encoder(image))
            image_embeds_frozen = image_embeds_frozen.float()
            image_atts = torch.ones(
                image_embeds_frozen.size()[:-1], dtype=torch.long
            ).to(self.device)
            query_tokens = self.query_tokens.expand(
                image_embeds_frozen.shape[0], -1, -1
            )
            query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(
                self.device
            )

            text = self.tokenizer(caption, return_tensors="pt", padding=True).to(
                self.device
            )
            attention_mask = torch.cat([query_atts, text.attention_mask], dim=1)

            output = self.Qformer.bert(
                text.input_ids,
                query_embeds=query_tokens,
                attention_mask=attention_mask,
                encoder_hidden_states=image_embeds_frozen,
                encoder_attention_mask=image_atts,
                return_dict=True,
            )

            multimodal_embeds = output.last_hidden_state[:, : query_tokens.size(1), :]

        return BlipOutputFeatures(
            image_embeds=image_embeds,
            image_embeds_proj=image_features,
            text_embeds=text_embeds,
            text_embeds_proj=text_features,
            multimodal_embeds=multimodal_embeds,
        )

    @classmethod
    def from_config(cls, cfg):
        vit_model = cfg.get("vit_model", "eva_clip_g")
        img_size = cfg.get("image_size")
        num_query_token = cfg.get("num_query_token")
        cross_attention_freq = cfg.get("cross_attention_freq", 2)

        drop_path_rate = cfg.get("drop_path_rate", 0)
        use_grad_checkpoint = cfg.get("use_grad_checkpoint", False)
        vit_precision = cfg.get("vit_precision", "fp16")
        freeze_vit = cfg.get("freeze_vit", True)

        max_txt_len = cfg.get("max_txt_len", 64)

        model = cls(
            vit_model=vit_model,
            img_size=img_size,
            drop_path_rate=drop_path_rate,
            use_grad_checkpoint=use_grad_checkpoint,
            vit_precision=vit_precision,
            freeze_vit=freeze_vit,
            num_query_token=num_query_token,
            cross_attention_freq=cross_attention_freq,
            max_txt_len=max_txt_len,
        )
        model.load_checkpoint_from_config(cfg)

        return model

    def compute_sim_matrix(self, data_loader, task_cfg):
        """
        Compute similarity i2t, t2i matrix for the given data loader.
        """
        k_test = task_cfg.k_test

        return compute_sim_matrix(model=self, data_loader=data_loader, k_test=k_test)
