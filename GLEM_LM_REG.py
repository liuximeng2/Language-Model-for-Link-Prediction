import argparse
from GLEM_util import *
from utils import *
import torch
from transformers import get_linear_schedule_with_warmup, AutoConfig
from transformers.models.auto import AutoModel, AutoTokenizer
from model import BertClassifier, BertClassifierReg, mlp_score, BertLoRAClassifier, BertLoRAClassifierInf
from transformers.trainer import Trainer, TrainingArguments
from evaluator import evaluate_hits, evaluate_mrr, evaluate_auc
from torch.utils.data import DataLoader
import numpy as np
from ogb.linkproppred import Evaluator
import logging
import warnings
import gc
import time
from peft import get_peft_model, LoraConfig, prepare_model_for_int8_training

# Record the start time
start_time = time.time()
warnings.filterwarnings("ignore", category=UserWarning)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class TextDataset(torch.utils.data.Dataset):
    def __init__(self, encodings, edge, num_node, pseudo_edge = None, pseudo_score = None, neg_edge = None):
        self.encodings = encodings
        self.edge = edge
        self.neg_edge = neg_edge
        self.pseudo_edge = pseudo_edge
        self.pseudo_score = pseudo_score
        self.num_node = num_node
        self.num_edge = edge.size()[0]

    def __getitem__(self, idx):
        selected_pos_nodes = self.edge[idx].flatten().to(torch.long)
        if self.neg_edge != None:
            selected_neg_nodes = self.neg_edge[idx].flatten().to(torch.long)
        else:
            selected_neg_nodes = torch.randperm(num_node)[:2].to(torch.long).to(selected_pos_nodes.device)
        pos_indice = torch.zeros(self.num_node).to(torch.bool)
        pos_indice[selected_pos_nodes] = True
        neg_indice = torch.zeros(self.num_node).to(torch.bool)
        neg_indice[selected_neg_nodes] = True
        input_ids = torch.cat((self.encodings['input_ids'][pos_indice], self.encodings['input_ids'][neg_indice]), dim = 0)
        attention_mask = torch.cat((self.encodings['attention_mask'][pos_indice], self.encodings['attention_mask'][neg_indice]), dim = 0)


        if self.pseudo_edge != None:
            idx = np.random.randint(self.pseudo_edge.size(0))
            sel_nodes = self.pseudo_edge[idx].flatten().to(torch.long)
            indice = torch.zeros(self.num_node).to(torch.bool)
            indice[sel_nodes] = True

            item = {
                'input_ids': input_ids,
                'attention_mask': attention_mask,
                'pseudo_input_ids': self.encodings['input_ids'][indice],
                'pseudo_attention_mask': self.encodings['attention_mask'][indice],
                'pseudo_score': self.pseudo_score[idx]
            } 
        else :
            item = {
            'input_ids': input_ids,
            'attention_mask': attention_mask
            }



        return item
    
    def __len__(self):
        return self.num_edge
    
def test(dataset, batch_size, model, token_max_length):
    model.eval()
    pos_preds = []
    neg_preds = []
    for i, data in enumerate(DataLoader(dataset, batch_size, drop_last=False, shuffle = False)):
            input_ids = data['input_ids'].to(device)
            attention_mask = data['attention_mask'].to(device)
            pos_output, neg_output, _= model(input_ids, attention_mask, batch_size, token_max_length)
            pos_preds += pos_output
            neg_preds += neg_output
    pos_preds = torch.cat(pos_preds, dim = 0)
    neg_preds = torch.cat(neg_preds, dim = 0)
    return pos_preds, neg_preds

parser = argparse.ArgumentParser(description='homo')
parser.add_argument('--data_name', type=str, default='cora')
parser.add_argument('--batch_size', type=int, default=4)

parser.add_argument('--lm_model', type=str, default='bert-small')
parser.add_argument('--gnn_model', type=str, default='GCN')
parser.add_argument('--edge_split_seed', type=int, default = 2)

parser.add_argument('--em_iter', type=int, default = 0)
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--alpha', type=float, default=0.2)
parser.add_argument('--regression', type=bool, default=True)
args = parser.parse_args()

data = load_data(args.data_name, args.edge_split_seed)
num_node = data.x.size(0)
train_edge, neg_train_edge = data.train_edges, data.train_edges_false
val_edge, neg_val_edge = data.val_edges, data.val_edges_false
test_edge, neg_test_edge = data.test_edges, data.test_edges_false
num_train_edge = train_edge.size()[0]
num_test_edge = test_edge.size()[0]
token_max_length = 512

if args.em_iter == 0:
    pesudo_data = torch.load(f'GLEM_model/GNN/{args.gnn_model}/embedding/{args.data_name}/reg({args.regression})/iter({args.em_iter})/es({args.edge_split_seed})/seed({args.seed})/lr(0.001)_do_(0.1)_seed({args.seed})')
else:
    # to be changed
    pesudo_data = torch.load(f'GLEM_model/GNN/{args.gnn_model}/embedding/{args.data_name}/reg({args.regression})/iter({args.em_iter})/es({args.edge_split_seed})/seed({args.seed})/lr(0.001)_do_(0.1)_beta({args.alpha})_seed({args.seed})')


pesudo_edge, pseudo_score = get_pseudo_score(data, pesudo_data)

log_path = f'Logs/GLEM_train/{args.data_name}/LM/reg({args.regression})/iter({args.em_iter})/seed({args.seed})/{args.lm_model}_es({args.edge_split_seed})_alpha{args.alpha}_seed{args.seed}_test.log'
init_path(log_path)
logging.basicConfig(filename=log_path, level=logging.INFO)
logging.info(f'EM-iter: {args.em_iter}')
logging.info(f'#train edge: {train_edge.size()}, #val edge: {val_edge.size()}, #test edge: {test_edge.size()}')

if args.lm_model == 'bert-base':
    config = AutoConfig.from_pretrained('model/pre_train/bert-base/')
    config.attention_probs_dropout_prob = 0.1
    config.hidden_dropout_prob = 0.3
    pretrained_model = AutoModel.from_pretrained("model/pre_train/bert-base/", config = config)
    tokenizer = AutoTokenizer.from_pretrained("model/tokenizer/bert-base/")
    X = tokenizer(data.raw_texts, padding=True, truncation=True, max_length=token_max_length, return_tensors='pt')
    in_channel = 768
if args.lm_model == 'bert-small':
    config = AutoConfig.from_pretrained('model/pre_train/bert-small/')
    config.attention_probs_dropout_prob = 0.1
    config.hidden_dropout_prob = 0.3
    pretrained_model = AutoModel.from_pretrained("model/pre_train/bert-small/", config = config)
    tokenizer = AutoTokenizer.from_pretrained("model/tokenizer/bert-base/")
    X = tokenizer(data.raw_texts, padding=True, truncation=True, max_length=token_max_length, return_tensors='pt')
    in_channel = 512
if args.lm_model == 'all-MiniLM-L6-v2':
    config = AutoConfig.from_pretrained('model/pre_trainall-MiniLM-L6-v2/')
    pretrained_model = AutoModel.from_pretrained("model/pre_train/all-MiniLM-L6-v2/", config = config)
    tokenizer = AutoTokenizer.from_pretrained("model/tokenizer/all-MiniLM-L6-v2/")
    X = tokenizer(data.raw_texts, padding = True, truncation=True, max_length=token_max_length, return_tensors = 'pt')
if args.lm_model == 'bigbird-roberta-base':
    config = AutoConfig.from_pretrained('model/pre_train/bigbird-roberta-base/')
    config.attention_probs_dropout_prob = 0.1
    config.hidden_dropout_prob = 0.3
    config.num_random_blocks = 3
    config.block_size = 32
    pretrained_model = AutoModel.from_pretrained("model/pre_train/bigbird-roberta-base/", config = config)
    tokenizer = AutoTokenizer.from_pretrained("model/tokenizer/bigbird-roberta-base/")
    X = tokenizer(data.raw_texts, padding=True, truncation=True, max_length=token_max_length, return_tensors='pt')
    in_channel = 768
if args.lm_model == 'longformer':
    config = AutoConfig.from_pretrained('model/pre_train/longformer/')
    config.attention_probs_dropout_prob = 0.1
    config.hidden_dropout_prob = 0.3
    pretrained_model = AutoModel.from_pretrained("model/pre_train/longformer/", config = config)
    tokenizer = AutoTokenizer.from_pretrained("model/tokenizer/longformer/")
    X = tokenizer(data.raw_texts, padding=True, truncation=True, max_length=token_max_length, return_tensors='pt')
    in_channel = 768
#loraConfig = LoraConfig(task_type= 'FEATURE_EXTRACTION', target_modules=['query', 'key', 'value'],inference_mode=False, r=8, lora_alpha=32, lora_dropout=0.05)
#pretrained_model = get_peft_model(pretrained_model, loraConfig)

#Creating training dataloaders
train_dataset = TextDataset(X, train_edge, num_node, pesudo_edge, pseudo_score, subseq_length = 64)
val_dataset = TextDataset(X, val_edge, num_node, neg_edge = neg_val_edge)
test_dataset = TextDataset(X, test_edge, num_node, neg_edge = neg_test_edge)
train_val_dataset = TextDataset(X, train_edge[:num_test_edge, :], num_node, neg_edge = neg_train_edge[:num_test_edge, :])

#Model parameters
num_tuned_layers = 4
hidden_layer = 256
num_layer = 2
mlp_dropout = 0.4

#Training parameters
runs = 1
batch_size = args.batch_size
num_epoch = 100
lr = 1e-5
l2 = 1e-4
kill_cnt_step = 5
eval_step = 5

label_running_loss = 0
pseudo_running_loss = 0
running_loss = 0
total_steps = (len(train_dataset) // batch_size) * num_epoch
warmup_step = int(0.1 * total_steps)

score_func = mlp_score(in_channel, hidden_layer, 1 , num_layer , mlp_dropout)
model = BertClassifierReg(pretrained_model, score_func, batch_size).to(device)
inf_model = BertClassifier(pretrained_model, score_func, batch_size).to(device)

#model.initialize(1)
for name, param in model.named_parameters():
    if param.requires_grad:
        print(name, param.size())

logging.info(f"Model: {args.lm_model}, Train/Val/Test: 85/5/10, args.edge_split_seed: {args.edge_split_seed}, num_epoch: {num_epoch},")
logging.info(f"# lm_tuned_layer: {num_tuned_layers}, lm_attention_dropout: {config.attention_probs_dropout_prob}, lm_hidden_dropout: {config.hidden_dropout_prob}, token length: {token_max_length}")
logging.info(f"scorefunc_dropout: {mlp_dropout}, scorefunc_hidden_layer: {hidden_layer}, scorefunc_num_layers: {num_layer}")
logging.info(f"batch_size: {batch_size}, learning_rate: {lr}, weight_decay: {l2}, kill_cnt: {kill_cnt_step}")
logging.info(f"Number of parameters: {parameter_counter(model)}")


score_save_path = f"GLEM_model/ft_lm/{args.lm_model}/embedding/{args.data_name}/reg({args.regression})/iter({args.em_iter})/es({args.edge_split_seed})/seed_{args.seed}/alpha({args.alpha})"
model_save_path = f"GLEM_model/ft_lm/{args.lm_model}/model/{args.data_name}/reg({args.regression})/iter({args.em_iter})/es({args.edge_split_seed})/seed_{args.seed}/alpha({args.alpha})"
init_path(score_save_path)
init_path(model_save_path)

evaluator_hit = Evaluator(name='ogbl-collab')
evaluator_mrr = Evaluator(name='ogbl-citation2')

loggers = {
    'MRR': Logger(runs),
    'Hits@1': Logger(runs),
    'Hits@3': Logger(runs),
    'Hits@10': Logger(runs),
    'Hits@100': Logger(runs)
}

global_best_result = 0

for run in range(runs):
    dummy = 0
    logging.info(f"-------------------------- run {run} --------------------------")

    early_stop = False
    best_result = 0
    kill_cnt = 0

    if runs == 1:
        seed = args.seed
    else:
        seed = run
    init_seed(seed)
    if args.em_iter == 0:
        pretrained_model = AutoModel.from_pretrained(f"model/pre_train/{args.lm_model}/", config = config)
        #model.reset_parameters(pretrained_model)
    else:
        model.load_state_dict(torch.load(f"GLEM_model/ft_lm/{args.lm_model}/model/{args.data_name}/reg({args.regression})/iter({args.em_iter - 1})/es({args.edge_split_seed})/seed_{args.seed}/alpha({args.alpha})"))
        #model.initialize(4)

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay= l2)
    #if args.em_iter == 0:
        #scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps = warmup_step, num_training_steps = total_steps)

    for epoch in range(1, 1 + num_epoch):
        for i, batch in enumerate(DataLoader(train_dataset, batch_size, drop_last=True, shuffle = True)):
            model.train()
            optimizer.zero_grad() 
            input_ids = batch['input_ids']
            attention_mask = batch['attention_mask']
            pseudo_input_ids = batch['pseudo_input_ids']
            pseudo_attention_mask = batch['pseudo_attention_mask']
            pseudo_score = batch['pseudo_score']
            
            _, _, loss_labeld, loss_pseudo = model(input_ids, attention_mask, pseudo_input_ids, pseudo_attention_mask, pseudo_score, token_max_length)
            loss = args.alpha * loss_pseudo + (1 - args.alpha) * loss_labeld

            tensor_memory = 0
            weight_memory = 0
            for obj in gc.get_objects():
                try:
                    if obj.is_cuda:
                        if (type(obj) == type(input_ids)):
                            tensor_memory += obj.element_size() * obj.nelement() / 1024 / 1024
                        else:
                            weight_memory += obj.element_size() * obj.nelement() / 1024 / 1024
                        print(f"Tensor: {obj.size()}, Type: {type(obj)}, Memory: {obj.element_size() * obj.nelement() / 1024 / 1024}")
                except Exception as e:
                    pass
            print(f"tensor_memory: {tensor_memory}")
            print(f"weight memory: {weight_memory}")

            print(torch.cuda.memory_allocated() / 1024 / 1024)
            print(torch.cuda.memory_reserved() / 1024 / 1024)
            loss.backward()

            if dummy == 0:
                grads = [p.grad for p in model.parameters() if p.grad is not None]
                grad_magnitudes = [torch.norm(g).item() for g in grads]
                logging.info(f"Gradient magnitudes: {grad_magnitudes}")
                dummy = dummy + 1

            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            #if args.em_iter == 0:
                #scheduler.step()
            label_running_loss += loss_labeld.item()
            pseudo_running_loss += loss_pseudo.item()
            running_loss += loss.item()

            print(torch.cuda.memory_allocated() / 1024 / 1024)
            print(torch.cuda.memory_reserved() / 1024 / 1024)
            os.exit(0)

        
        if (epoch + 1) % eval_step == 0:
            end_time = time.time()

            # Calculate the total run time
            total_time = end_time - start_time

            logging.info(f"Total run time: {total_time} seconds")
            print(f"Total run time: {total_time} seconds")

            with torch.no_grad():
                inf_model.load_state_dict(model.state_dict())
                inf_model.eval()
                train_pos_preds, train_neg_preds = test(train_val_dataset, batch_size, inf_model, token_max_length)
                val_pos_preds, val_neg_preds = test(val_dataset, batch_size, inf_model, token_max_length)
                test_pos_preds, test_neg_preds = test(test_dataset, batch_size, inf_model, token_max_length)

            train_pos_preds = torch.flatten(train_pos_preds)
            val_pos_preds = torch.flatten(val_pos_preds)
            test_pos_preds = torch.flatten(test_pos_preds)
            train_neg_preds = torch.flatten(train_neg_preds)
            val_neg_preds = torch.flatten(val_neg_preds)
            test_neg_preds = torch.flatten(test_neg_preds)
            score_emb = [val_pos_preds.cpu(),val_neg_preds.cpu(), test_pos_preds.cpu(), test_neg_preds.cpu()]

            result = {}
            k_list = [1, 3, 10, 100]

            result_mrr_train = evaluate_mrr(evaluator_mrr, train_pos_preds, train_neg_preds.repeat(train_neg_preds.size(0), 1))
            result_mrr_val = evaluate_mrr(evaluator_mrr, val_pos_preds, val_neg_preds.repeat(val_neg_preds.size(0), 1))
            result_mrr_test = evaluate_mrr(evaluator_mrr, test_pos_preds, test_neg_preds.repeat(test_neg_preds.size(0), 1))
            result['MRR'] = (result_mrr_train['MRR'], result_mrr_val['MRR'], result_mrr_test['MRR'])

            for K in [1,3,10, 100]:
                result[f'Hits@{K}'] = (result_mrr_train[f'mrr_hit{K}'], result_mrr_val[f'mrr_hit{K}'], result_mrr_test[f'mrr_hit{K}'])

            for key, results in result.items():

                loggers[key].add_result(run, results)
                            
                if key == 'MRR':
                    
                    train_hits, valid_hits, test_hits = results
                    logging.info(
                                f'Epoch: {epoch + 1}, '
                                f'train loss: {running_loss /(i * eval_step):.3f}, '
                                f'label loss: {label_running_loss /(i * eval_step):.3f}, '
                                f'pseudo loss: {pseudo_running_loss /(i * eval_step):.3f}, '
                                f'Train_mrr: {100 * train_hits:.2f}%, '
                                f'Valid_mrr: {100 * valid_hits:.2f}%, '
                                f'Test_mrr: {100 * test_hits:.2f}%')

                    if 100 * valid_hits > best_result:
                        best_result = 100 * valid_hits
                        kill_cnt = 0
                        save_emb(score_emb, score_save_path)
                        torch.save(model.state_dict(), model_save_path)
                        
                    else:
                        kill_cnt += 1

                        if kill_cnt > kill_cnt_step: 
                            logging.info("Early Stopping!!")
                            logging.info(f'Final test mrr: {100 * test_hits:.2f}, Best test mrr: {best_result:.2f}')
                            early_stop = True
                            break

            if early_stop == True:
                break

            running_loss = 0
            pseudo_running_loss = 0
            label_running_loss = 0

#Show result
for key in loggers.keys():
    #logging.info(key)
    #logging.info(loggers[key].print_statistics())
    best_metric,  best_valid_mean, mean_list, var_list = loggers[key].print_statistics()
    logging.info(f'{key} result: Train: {mean_list[0]} ± {var_list[0]}, Valid: {mean_list[1]} ± {var_list[1]}, Test: {mean_list[2]} ± {var_list[2]}')
