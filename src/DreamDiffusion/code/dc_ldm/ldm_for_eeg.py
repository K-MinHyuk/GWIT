import numpy as np
import wandb
import torch
from dc_ldm.util import instantiate_from_config
from omegaconf import OmegaConf
import torch.nn as nn
import os
from dc_ldm.models.diffusion.plms import PLMSSampler
from einops import rearrange, repeat
from torchvision.utils import make_grid
from torch.utils.data import DataLoader
import torch.nn.functional as F
from sc_mbm.mae_for_eeg import eeg_encoder, classify_network, mapping 
from PIL import Image
def create_model_from_config(config, num_voxels, global_pool):
    model = eeg_encoder(time_len=num_voxels, patch_size=config.patch_size, embed_dim=config.embed_dim,
                depth=config.depth, num_heads=config.num_heads, mlp_ratio=config.mlp_ratio, global_pool=global_pool) 
    return model

def contrastive_loss(logits, dim):
    neg_ce = torch.diag(F.log_softmax(logits, dim=dim))
    return -neg_ce.mean()
    
def clip_loss(similarity: torch.Tensor) -> torch.Tensor:
    caption_loss = contrastive_loss(similarity, dim=0)
    image_loss = contrastive_loss(similarity, dim=1)
    return (caption_loss + image_loss) / 2.0


#### COND STAGE MODEL Originale ####

class cond_stage_model(nn.Module):
    def __init__(self, metafile, num_voxels=440, cond_dim=1280, global_pool=True, clip_tune = True,
                 cls_tune = False, encoder_name='loro'):
        super().__init__()
        # prepare pretrained fmri mae 
        if metafile is not None:
            print("Loading encoder from checkpoint")
            model = create_model_from_config(metafile['config'], num_voxels, global_pool)
            #commentato
            model.load_checkpoint(metafile['model'])
        else:
            print("Initializing encoder from scratch")
            model = eeg_encoder(time_len=num_voxels, global_pool=global_pool)
        self.mae = model
        if clip_tune:
            self.mapping = mapping(encoder_name=encoder_name)
        if cls_tune:
            self.cls_net = classify_network()

        self.fmri_seq_len = model.num_patches
        self.fmri_latent_dim = model.embed_dim
        if global_pool == False:
            self.channel_mapper = nn.Sequential(
                nn.Conv1d(self.fmri_seq_len, self.fmri_seq_len // 2, 1, bias=True),
                nn.Conv1d(self.fmri_seq_len // 2, 77, 1, bias=True)
            )
        self.dim_mapper = nn.Linear(self.fmri_latent_dim, cond_dim, bias=True)
        self.global_pool = global_pool

        # self.image_embedder = FrozenImageEmbedder()

    # def forward(self, x):
    #     # n, c, w = x.shape
    #     latent_crossattn = self.mae(x)
    #     if self.global_pool == False:
    #         latent_crossattn = self.channel_mapper(latent_crossattn)
    #     latent_crossattn = self.dim_mapper(latent_crossattn)
    #     out = latent_crossattn
    #     return out

    def forward(self, x):
        # n, c, w = x.shape
        latent_crossattn = self.mae(x)
        latent_return = latent_crossattn
        if self.global_pool == False:
            latent_crossattn = self.channel_mapper(latent_crossattn)
        latent_crossattn = self.dim_mapper(latent_crossattn)
        out = latent_crossattn
        return out, latent_return

    # def recon(self, x):
    #     recon = self.decoder(x)
    #     return recon

    def get_cls(self, x):
        return self.cls_net(x)

    def get_clip_loss(self, x, image_embeds):
        # image_embeds = self.image_embedder(image_inputs)
        target_emb = self.mapping(x)
        # similarity_matrix = nn.functional.cosine_similarity(target_emb.unsqueeze(1), image_embeds.unsqueeze(0), dim=2)
        # loss = clip_loss(similarity_matrix)
        loss = 1 - torch.cosine_similarity(target_emb, image_embeds, dim=-1).mean()
        return loss


### COND STAGE MODEL CON BENDR ####
import sys
# sys.path.append('../../../BENDR')
sys.path.append('./src/BENDR')
from dn3_ext import ConvEncoderBENDR, BENDRContextualizer

class cond_stage_model(nn.Module):
    def __init__(self, metafile, num_voxels=440, cond_dim=1280, global_pool=True, clip_tune = True, cls_tune = False, encoder_name='bendr'):
        super().__init__()
        # prepare pretrained fmri mae 
        if metafile is not None:
            print("Loading encoder from checkpoint")

            

            bendr_encoder  = ConvEncoderBENDR(in_features=128, 
                               encoder_h=512, 
                               enc_downsample=[3, 2] , 
                               enc_width=[3, 2] )
            encoded_samples = bendr_encoder.downsampling_factor(1026)
            mask_t_span = int(0.1 * encoded_samples)
            mask_c_span = int(0.1 * 512)
            contextualizer = contextualizer = BENDRContextualizer(512, finetuning=True,
                                                  mask_p_t=0.01, mask_p_c=0.005, layer_drop=0,
                                                  mask_c_span=mask_c_span, dropout=0.,
                                                  mask_t_span=mask_t_span, normal=False)
            bendr_encoder.load(metafile, strict=True)
            metafile_cont = metafile.replace('encoder', 'contextualizer')
            contextualizer.load(metafile_cont, strict=True)
            
            # model.freeze_features()
            bendr_encoder = bendr_encoder.to('cuda')
            contextualizer = contextualizer.to('cuda')
        else:
            print("Initializing encoder from scratch")
            bendr_encoder = ConvEncoderBENDR(in_features=20, encoder_h=512)
        
        self.mae = nn.Sequential(bendr_encoder, contextualizer)
        self.fmri_latent_dim = 1536 #512 #model.encoder_h
        self.fmri_seq_len = 75# 74
    
        # self.fmri_seq_len = model.num_patches
        # self.fmri_latent_dim = model.embed_dim
        if clip_tune:
            self.mapping = mapping(self.fmri_seq_len,  self.fmri_latent_dim, encoder_name)
        if cls_tune:
            self.cls_net = classify_network()


        if global_pool == False:
            self.channel_mapper = nn.Sequential(
                nn.Conv1d(self.fmri_seq_len, self.fmri_seq_len // 2, 1, bias=True),
                nn.Conv1d(self.fmri_seq_len // 2, 77, 1, bias=True)
            )
        self.dim_mapper = nn.Linear(self.fmri_latent_dim, cond_dim, bias=True)
        self.global_pool = global_pool

        # self.image_embedder = FrozenImageEmbedder()


    def forward(self, x):
        # n, c, w = x.shape
        latent_crossattn = self.mae(x) #torch.Size([3, 512, 75]) o senza conv1d torch.Size([75, 3, 1536])
        #bender has shape inverted
        latent_crossattn = latent_crossattn.permute(1, 0, 2)
        # print("latent_crossattn: ", latent_crossattn.shape) # torch.Size([5, 128, 1024])
        latent_return = latent_crossattn
        if self.global_pool == False:
            latent_crossattn = self.channel_mapper(latent_crossattn)
            # print("latent_crossattn after channel mapper: ", latent_crossattn.shape) # torch.Size([5, 77, 1024])
        latent_crossattn = self.dim_mapper(latent_crossattn)
        # print("latent_crossattn after dim mapper: ", latent_crossattn.shape) # torch.Size([5, 77, 768])
        out = latent_crossattn
        return out, latent_return

    # def recon(self, x):
    #     recon = self.decoder(x)
    #     return recon

    def get_cls(self, x):
        return self.cls_net(x)

    def get_clip_loss(self, x, image_embeds):
        #image embeds shape (#,768)
        #x shape (#,74, 512)
        # image_embeds = self.image_embedder(image_inputs) 
        #x.shape (#,77,768)
        target_emb = self.mapping(x)
        # similarity_matrix = nn.functional.cosine_similarity(target_emb.unsqueeze(1), image_embeds.unsqueeze(0), dim=2)
        # loss = clip_loss(similarity_matrix)
        loss = 1 - torch.cosine_similarity(target_emb, image_embeds, dim=-1).mean()
        return loss
    


class eLDM:

    def __init__(self, metafile, num_voxels, device=torch.device('cpu'),
                 pretrain_root='../pretrains/',
                 logger=None, ddim_steps=250, global_pool=True, use_time_cond=False, clip_tune = True, cls_tune = False):
        # self.ckp_path = os.path.join(pretrain_root, 'model.ckpt')
        # self.ckp_path = os.path.join(pretrain_root, 'models/v1-5-pruned.ckpt')
        self.ckp_path = 'src/DreamDiffusion/pretrains/models/v1-5-pruned.ckpt'
        self.config_path = 'src/DreamDiffusion/pretrains/models/config15.yaml' #os.path.join(pretrain_root, 'models/config15.yaml') 
        config = OmegaConf.load(self.config_path)
        config.model.params.unet_config.params.use_time_cond = use_time_cond
        config.model.params.unet_config.params.global_pool = global_pool

        self.cond_dim = config.model.params.unet_config.params.context_dim
        print("cond_dim: ", self.cond_dim)

        model = instantiate_from_config(config.model)
        pl_sd = torch.load(self.ckp_path, map_location="cpu")['state_dict']
        # print("state sd: ", torch.load(self.ckp_path, map_location="cpu")['state'])
       
        m, u = model.load_state_dict(pl_sd, strict=False)
        model.cond_stage_trainable = True
        model.cond_stage_model = cond_stage_model(metafile, num_voxels, self.cond_dim, global_pool=global_pool, clip_tune = clip_tune,cls_tune = cls_tune)

        model.ddim_steps = ddim_steps
        model.re_init_ema()
        if logger is not None:
            logger.watch(model, log="all", log_graph=False)

        model.p_channels = config.model.params.channels
        model.p_image_size = config.model.params.image_size
        model.ch_mult = config.model.params.first_stage_config.params.ddconfig.ch_mult

        
        self.device = device    
        self.model = model
        
        self.model.clip_tune = clip_tune
        self.model.cls_tune = cls_tune

        self.ldm_config = config
        self.pretrain_root = pretrain_root
        self.fmri_latent_dim = model.cond_stage_model.fmri_latent_dim
        self.metafile = metafile

    def finetune(self, trainers, dataset, test_dataset, bs1, lr1,
                output_path, config=None):
        config.trainer = None
        config.logger = None
        self.model.main_config = config
        self.model.output_path = output_path
        # self.model.train_dataset = dataset
        self.model.run_full_validation_threshold = 0.15
        # stage one: train the cond encoder with the pretrained one
      
        # # stage one: only optimize conditional encoders
        print('\n##### Stage One: only optimize conditional encoders #####')
        def collate_fn(batch):
            batch = list(filter(lambda x: x is not None, batch))
            return torch.utils.data.dataloader.default_collate(batch)
        dataloader = DataLoader(dataset, batch_size=bs1, shuffle=True, num_workers=12, collate_fn=collate_fn)
        test_loader = DataLoader(test_dataset, batch_size=bs1, shuffle=False, num_workers=12, collate_fn=collate_fn)
        self.model.unfreeze_whole_model()
        self.model.freeze_first_stage()
        # self.model.freeze_whole_model()
        # self.model.unfreeze_cond_stage()

        print("Train samples: ", len(dataset))
        print("Test samples: ", len(test_dataset))

        self.model.learning_rate = lr1
        self.model.train_cond_stage_only = True
        self.model.eval_avg = config.eval_avg
        trainers.fit(self.model, dataloader, val_dataloaders=test_loader)

        self.model.unfreeze_whole_model()
        
        torch.save(
            {
                'model_state_dict': self.model.state_dict(),
                'config': config,
                'state': torch.cuda.get_rng_state()

            },
            os.path.join(output_path, 'checkpoint.pth')
        )
        

    @torch.no_grad()
    def generate(self, fmri_embedding, num_samples, ddim_steps, HW=None, limit=None, state=None, output_path = None):
        # fmri_embedding: n, seq_len, embed_dim
        all_samples = []
        if HW is None:
            shape = (self.ldm_config.model.params.channels, 
                self.ldm_config.model.params.image_size, self.ldm_config.model.params.image_size)
        else:
            num_resolutions = len(self.ldm_config.model.params.first_stage_config.params.ddconfig.ch_mult)
            shape = (self.ldm_config.model.params.channels,
                HW[0] // 2**(num_resolutions-1), HW[1] // 2**(num_resolutions-1))

        model = self.model.to(self.device)
        sampler = PLMSSampler(model)
        # sampler = DDIMSampler(model)
        if state is not None:
            torch.cuda.set_rng_state(state)
            
        with model.ema_scope():
            model.eval()
            for count, item in enumerate(fmri_embedding):
                if limit is not None:
                    if count >= limit:
                        break
                # print(item)
                latent = item['eeg']
                # Prova passando tensore random invece di eeg
                # print("eeg: ", latent)
                # latent = torch.randn(latent.shape).to(self.device)
                # print("fake eeg: ", latent)

                gt_image = rearrange(item['image'], 'h w c -> 1 c h w') # h w c
                print(f"rendering {num_samples} examples in {ddim_steps} steps.")
                # assert latent.shape[-1] == self.fmri_latent_dim, 'dim error'
                
                c, re_latent = model.get_learned_conditioning(repeat(latent, 'h w -> c h w', c=num_samples).to(self.device))
                #added to see clip loss
                item['image_raw']['pixel_values'] = item['image_raw']['pixel_values'].unsqueeze(0)
                image_embeds = model.image_embedder(item['image_raw'].to(self.device))
                loss_clip = model.cond_stage_model.get_clip_loss(c, image_embeds)
                wandb.log({"clip_loss": loss_clip})
                
                # c = model.get_learned_conditioning(repeat(latent, 'h w -> c h w', c=num_samples).to(self.device))
                samples_ddim, _ = sampler.sample(S=ddim_steps, 
                                                conditioning=c,
                                                batch_size=num_samples,
                                                shape=shape,
                                                verbose=False,
                                                unconditional_guidance_scale=1
                                                )

                x_samples_ddim = model.decode_first_stage(samples_ddim)
                x_samples_ddim = torch.clamp((x_samples_ddim+1.0)/2.0, min=0.0, max=1.0)
                gt_image = torch.clamp((gt_image+1.0)/2.0, min=0.0, max=1.0)
                
                all_samples.append(torch.cat([gt_image, x_samples_ddim.detach().cpu()], dim=0)) # put groundtruth at first
                if output_path is not None:
                    samples_t = (255. * torch.cat([gt_image, x_samples_ddim.detach().cpu()], dim=0).numpy()).astype(np.uint8)
                    for copy_idx, img_t in enumerate(samples_t):
                        img_t = rearrange(img_t, 'c h w -> h w c')
                        Image.fromarray(img_t).save(os.path.join(output_path, 
                            f'./test{count}-{copy_idx}.png'))
        
        # display as grid
        grid = torch.stack(all_samples, 0)
        grid = rearrange(grid, 'n b c h w -> (n b) c h w')
        grid = make_grid(grid, nrow=num_samples+1)

        # to image
        grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
        model = model.to('cpu')
        
        return grid, (255. * torch.stack(all_samples, 0).cpu().numpy()).astype(np.uint8)