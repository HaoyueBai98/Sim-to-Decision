import sys
import os
import time
import random
import faiss
import torch
import wandb
import numpy as np
from torch import autograd
from tqdm import tqdm
from tools.utils import AverageMeter
from tools import feature_list
from evaluations.metric import compute_rec_loss, compute_error_rates, weighted_label_smoothing_loss, loss_function, focal_loss
from torch.utils.data import DataLoader
from tools.logger import info
from sklearn.preprocessing import StandardScaler
import torch.nn.functional as F

class CB_Session(object):
    def __init__(self, env, model, dataset):
        self.env = env
        self.model = model
        self.value_network = None
        self.optimizer_dm = None
        self.loader = DataLoader(dataset, batch_size=self.env.args.batch_size, shuffle=True)
        self.train_inputs = dataset.train_inputs
        self.val_inputs = dataset.val_inputs
        self.test_inputs = dataset.test_inputs
        self.optimizer = torch.optim.Adam(
            [{'params': filter(lambda p: p.requires_grad, self.model.parameters()), 'lr': self.env.args.lr}], weight_decay=self.env.args.decay_coeff)
        

        self.action_dim = 4
        self.epsilon = 0.1
        
        self.early_stop = 0
        self.best_epoch = 0
        self.best_dm_epoch = 0
        self.total_epoch = 0
        self.best_overall_accuracy = 0
        self.best_acc1, self.best_acc2, self.best_acc3 = 0, 0, 0
        self.best_dm_accuracy = 0
        self.cost_dic = dataset.cost_mrp
        self.avg_profit = dataset.avg_profit
        self.test_rec_loss = 99999
        self.scaler = StandardScaler()
        self.best_p = 0
        self.best_o = 0
        self.best_pmp1 = 0
        self.best_pmp2 = 0
        self.best_pmp3 = 0

        self.min_profit, self.max_profit = float('inf'), float('-inf')
        self.min_on_time, self.max_on_time = float('inf'), float('-inf')

    def init_value_network(self, value_network):
        self.value_network = value_network
        self.optimizer_dm = torch.optim.Adam(
            [
                
                {'params': self.value_network.parameters(), 'lr': self.env.args.dm_lr}
            ]
            , weight_decay=self.env.args.dm_decay_coeff
        )

    def train_epoch(self):
        t = time.time()
        self.model.train()
        self.total_epoch += 1

        all_rec_loss = AverageMeter()
        all_kl_loss = AverageMeter()
        all_classification_loss = AverageMeter()

        

        feature_dim = len(feature_list.product_info[self.env.args.dataset] + feature_list.order_info[self.env.args.dataset] +\
                           feature_list.customer_info[self.env.args.dataset] + feature_list.shipping_info[self.env.args.dataset] )

        label_dim =  len(feature_list.label[self.env.args.dataset])

        for input_id in tqdm(self.loader):
            
            
            ori_input = input_id.to(self.env.device)
            input_id = self.scaler.fit_transform(input_id)
            input_id = torch.FloatTensor(input_id).to(self.env.device)


            predicted_tokens = self.model(input_id[:,:feature_dim], ori_input[:,feature_dim].long(), ori_input[:,feature_dim+1:].long())
            total_loss = 0
            for i in range(label_dim):
                classification_loss = torch.nn.CrossEntropyLoss()(predicted_tokens[i], ori_input[:, feature_dim+1 + i].long())
                total_loss += classification_loss

            loss = total_loss / label_dim
            
            self.optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1)
            self.optimizer.step()
        return all_classification_loss.avg, time.time() - t
 


    def train(self):
        self.early_stop = 0
        for epoch in range(self.env.args.ckpt_start_epoch, self.env.args.epochs):
            classification_loss, train_time = self.train_epoch()
            info('-' * 50)
            info(
                f'TRAIN:epoch = {epoch}/{self.env.args.epochs} classification_loss = {classification_loss:.5f} train_time = {train_time:.2f}')
            if self.env.args.wandb:
                wandb.log({"loss/classification_loss":classification_loss}, epoch)
            self.test('val')
            if epoch % self.env.args.eva_interval == 0:
                self.early_stop += 1
                accuracies, val_time = self.test('val')
                
                info('-' * 10)
                
                
                for i, accuracy in enumerate(accuracies):
                    info(f"{feature_list.label[self.env.args.dataset][i]} Accuracy: {accuracy * 100:.2f}% val_time = {val_time:.2f}")
                    if self.env.args.wandb:
                        wandb.log({f"eval/{feature_list.label[self.env.args.dataset][i]}":accuracy}, epoch)

                if (sum(accuracies) / len(accuracies) if accuracies else 0) > self.best_overall_accuracy:
                    info('-' * 10)
                    self.best_overall_accuracy = sum(accuracies) / len(accuracies) if accuracies else 0
                    self.best_acc1 = accuracies[0]
                    self.best_acc2 = accuracies[1]
                    self.best_acc3 = accuracies[2]
                    if self.env.args.wandb:
                        wandb.log({f"eval/best_overall_accuracy":self.best_overall_accuracy}, epoch)
                    info(f"best_overall_accuracy: {self.best_overall_accuracy * 100:.2f}% ")
                    self.early_stop = 0
                    if self.env.args.save:
                        self.save_model(epoch, 'sim')
                    self.best_epoch = epoch
                    
            if self.early_stop > self.env.args.early_stop:
                break


        
    def dm_train_epoch(self):
        t = time.time()
        self.model.train()
        self.value_network.train()
        self.total_epoch += 1

        all_mi_loss = AverageMeter()

        all_ma_loss = AverageMeter()
        all_profit = AverageMeter()
        all_on_time = AverageMeter()

        all_profit_loss = AverageMeter()
        all_late_loss = AverageMeter()
        all_loss = AverageMeter()

        feature_dim = len(feature_list.product_info[self.env.args.dataset] + feature_list.order_info[self.env.args.dataset] +\
                           feature_list.customer_info[self.env.args.dataset] + feature_list.shipping_info[self.env.args.dataset] )

        self.cost_dic_data = self.cost_dic[:, :-1] 
        self.cost_dic_y = self.cost_dic[:, -1] 

        index = faiss.IndexFlatL2(self.cost_dic_data.shape[1]) 
        index.add(self.cost_dic_data)

        for param in self.model.parameters():
            param.requires_grad = False

        for input_id in tqdm(self.loader):

            ori_input = input_id.to(self.env.device)
            input_id = self.scaler.fit_transform(input_id)
            input_id = torch.FloatTensor(input_id).to(self.env.device)
            state = input_id[:, :feature_dim]


            decision_prob_value = F.softmax(self.value_network(state), dim=1)  
            decision_prob = F.gumbel_softmax(decision_prob_value, tau=1, hard=True)
            


            selected_embedding = torch.sum(decision_prob.unsqueeze(2) * self.model.embedding.weight[:4, :], dim=1)
            predicted_tokens = self.model(input_id[:,:feature_dim], selected_embedding, ori_input[:,feature_dim+1:])
        

            profits = torch.tensor(self.avg_profit)
            weights = profits / profits.max()

            target_class_decision = torch.argmax(profits).expand(decision_prob.size(0)).to(self.env.device)

            decision_weights = weights.to(self.env.device)

            profit_loss = F.cross_entropy(decision_prob, target_class_decision, weight=decision_weights)

            target_class_predicted = torch.ones(predicted_tokens[-1].size(0), dtype=torch.long).to(self.env.device)

            late_loss = F.cross_entropy(predicted_tokens[-1], target_class_predicted)

            mi_loss = self.env.args.mip_coeff * profit_loss + self.env.args.mil_coeff * late_loss
            
            all_profit_loss.update(self.env.args.mip_coeff * profit_loss)
            all_late_loss.update(self.env.args.mil_coeff * late_loss)

            action = decision_prob.argmax(dim=1).squeeze() 

            query_vectors = np.array([
                [
                    ori_input[i, feature_list.retrieva_index[self.env.args.dataset][0]].cpu().item(),
                    ori_input[i, feature_list.retrieva_index[self.env.args.dataset][1]].cpu().item(),
                    action[i].item()
                ]
                for i in range(len(state))
            ], dtype='float32')

            _, nearest_indices = index.search(query_vectors, 1)  
            nearest_samples = self.cost_dic_data[nearest_indices.flatten()].cpu().numpy()  

            action_profit_sum = torch.zeros(self.action_dim, device=self.env.device)  
            action_profit_count = torch.zeros(self.action_dim, device=self.env.device) 
            action_on_time_sum = torch.zeros(self.action_dim, device=self.env.device) 
            action_on_time_count = torch.zeros(self.action_dim, device=self.env.device) 

            query_vectors_tensor = torch.tensor(query_vectors, device=self.env.device) 
            nearest_samples_tensor = torch.tensor(nearest_samples, device=self.env.device)

            matches = torch.all(query_vectors_tensor == nearest_samples_tensor, dim=1)  

            selected_y = torch.where(
                matches,
                self.cost_dic_y[nearest_indices.flatten()].to(self.env.device),
                torch.tensor(self.avg_profit, device=self.env.device)[action]
            )

            one_hot_action = F.one_hot(action, num_classes=self.action_dim).float()
            action_profit_sum = torch.matmul(one_hot_action.T, selected_y.unsqueeze(1)).squeeze(1) 
            action_profit_count = one_hot_action.sum(dim=0) 

            on_time = predicted_tokens[-1].argmax(dim=1) 
            action_on_time_sum = torch.matmul(one_hot_action.T, on_time.unsqueeze(1).float()).squeeze(1) 
            action_on_time_count = action_profit_count 

            avg_profit_per_action = action_profit_sum / (action_profit_count + 1e-8) 
            avg_on_time_per_action = action_on_time_sum / (action_on_time_count + 1e-8)  

            reward_per_action =  avg_profit_per_action + self.env.args.otr_reward_coeff * avg_on_time_per_action 


            self.optimizer_dm.zero_grad()

            if not hasattr(self, 'smoothed_reward'):
                self.smoothed_reward = reward_per_action

            batch_size = state.shape[0]

            self.smoothed_reward =  self.env.args.reward_smoothing_factor * reward_per_action + \
                (1 - self.env.args.reward_smoothing_factor) * self.smoothed_reward

            predicted_rewards = self.value_network(state).mean(dim=0)

            ma_loss = F.mse_loss(predicted_rewards, self.smoothed_reward)
            loss = self.env.args.mi_coeff * mi_loss + self.env.args.ma_coeff * ma_loss

            loss.backward()
            self.optimizer_dm.step()


            all_mi_loss.update(self.env.args.mi_coeff * mi_loss)
            all_ma_loss.update(self.env.args.ma_coeff * ma_loss)
            all_loss.update(loss, len(input_id))
           

        return all_loss.avg, all_profit_loss.avg, all_late_loss.avg, all_profit.avg, all_on_time.avg, all_mi_loss.avg, all_ma_loss.avg, time.time() - t

    

    def dm_train(self):
        self.early_stop = 0

        for epoch in range(self.env.args.dm_epochs):

            loss, profit_loss, late_loss, profit_r, on_time_r, mi_loss, ma_loss, train_time = self.dm_train_epoch()
            info('-' * 50)
            info(
                f'TRAIN:epoch = {epoch}/{self.env.args.dm_epochs} loss = {loss:.5f} profit_loss = {profit_loss:.5f} late_loss = {late_loss:.5f} train_time = {train_time:.2f}')
            info(
                f'profit = {profit_r:.5f} on_time = {on_time_r:.5f} mi_loss = {mi_loss:.5f} ma_loss = {ma_loss:.5f}')
            if self.env.args.wandb:
                wandb.log({"loss/loss":loss}, self.env.args.epochs + 1 + epoch)

                wandb.log({"loss/profit_loss":profit_loss}, self.env.args.epochs + 1 + epoch)
                wandb.log({"loss/late_loss":late_loss}, self.env.args.epochs + 1 + epoch)

                wandb.log({"loss/profit":profit_r}, self.env.args.epochs + 1 + epoch)
                wandb.log({"loss/on_time":on_time_r}, self.env.args.epochs + 1 + epoch)

                wandb.log({"loss/mi_loss":mi_loss}, self.env.args.epochs + 1 + epoch)
                wandb.log({"loss/ma_loss":ma_loss}, self.env.args.epochs + 1 + epoch)

            if epoch % self.env.args.eva_interval == 0:
                self.early_stop += 1
                profit, on_time_ratio, profit_min_percent, val_time = self.dm_test('val')
                info('-' * 10)
                info(
                    f'avg_profit = {profit:.5f} on_time_ratio = {on_time_ratio:.5f} overall = {profit+on_time_ratio:.5f} val_time = {val_time:.2f}')
                info(f'profit_min_percent_10 = {profit_min_percent[0.1]:.5f} profit_min_percent_20 = {profit_min_percent[0.2]:.5f} profit_min_percent_30 = {profit_min_percent[0.3]:.5f}')
    
                
                if self.env.args.wandb:
                        wandb.log({f"eval/avg_profit":profit, 'eval/on_time_ratio':on_time_ratio}, self.env.args.epochs + 1 + epoch)
                        wandb.log({f"eval/profit_min_percent_10":profit_min_percent[0.1], \
                                   'eval/profit_min_percent_20':profit_min_percent[0.2], \
                                    'eval/profit_min_percent_30':profit_min_percent[0.3]}, self.env.args.epochs + 1 + epoch)

                if on_time_ratio + profit > self.best_dm_accuracy:
                    self.best_dm_accuracy =  on_time_ratio + profit
                    self.best_p = profit
                    self.best_o = on_time_ratio
                    self.best_pmp1 = profit_min_percent[0.1]
                    self.best_pmp2 = profit_min_percent[0.3]
                    self.best_pmp3 = profit_min_percent[0.3]


                    if self.env.args.wandb:
                        wandb.log({f"eval/best_on_time_ratio":on_time_ratio}, self.env.args.epochs + 1 + epoch)
                        wandb.log({f"eval/best_profit":profit},self.env.args.epochs + 1 + epoch)
                        wandb.log({f"eval/best_dm_accuracy":self.best_dm_accuracy},self.env.args.epochs + 1 + epoch)

                    info(f"best_dm_accuracy: {self.best_dm_accuracy:.5f} ")

                    self.early_stop = 0
                    if self.env.args.save:
                        self.save_model(self.env.args.epochs + 1 + epoch, 'dm')
                    self.best_dm_epoch = self.env.args.epochs + 1 + epoch
                    
            if self.early_stop > self.env.args.early_stop:
                break




    def dm_test(self, mode):
        self.model.eval()  
        self.value_network.eval()
        t = time.time()

        if mode == 'val' or mode == 'ori':
            input_id = self.val_inputs
        else:
            input_id = self.test_inputs

        ori_input = input_id.to(self.env.device)

        if mode != 'ori':
            input_id = self.scaler.transform(input_id)

        input_id = torch.FloatTensor(input_id).to(self.env.device)
        feature_dim = len(feature_list.product_info[self.env.args.dataset] + feature_list.order_info[self.env.args.dataset] +\
                        feature_list.customer_info[self.env.args.dataset] + feature_list.shipping_info[self.env.args.dataset])

        self.cost_dic_data = self.cost_dic[:, :-1]  
        self.cost_dic_y = self.cost_dic[:, -1] 

        index = faiss.IndexFlatL2(self.cost_dic_data.shape[1])  
        index.add(self.cost_dic_data) 

        profit_sum = 0 
        profit_count = 0 
        time_sum = 0 
        time_count = 0 
        local_profits = []

        state = input_id[:, :feature_dim]

        with torch.no_grad():
            if mode == 'ori':
                decision_indices = input_id[:, feature_dim].long() 
                decision_prob = F.one_hot(decision_indices, num_classes=4).float().to(self.env.device)
                decision_prob_value = decision_prob
            else:
                value_network_output = self.value_network(state)
                decision_prob_value =(F.softmax(value_network_output, dim=1) == F.softmax(value_network_output, dim=1).max(dim=1, keepdim=True).values).float() 
                decision_prob = decision_prob_value

            action = decision_prob_value.argmax(dim=1).squeeze()  

            query_vectors = np.array([
                [
                    ori_input[i, feature_list.retrieva_index[self.env.args.dataset][0]].cpu().item(),
                    ori_input[i, feature_list.retrieva_index[self.env.args.dataset][1]].cpu().item(),
                    action[i].item()
                ]
                for i in range(len(state))
            ], dtype='float32')

            _, nearest_indices = index.search(query_vectors, 1)  
            nearest_samples = self.cost_dic_data[nearest_indices.flatten()].cpu().numpy() 

            for idx, (query, nearest) in enumerate(zip(query_vectors, nearest_samples)):
                if np.array_equal(query, nearest):
                    selected_y = self.cost_dic_y[nearest_indices[idx, 0]]
                else:
                    selected_y = torch.tensor(self.avg_profit)[action[idx].cpu()]

                profit_sum += selected_y
                profit_count += 1
                local_profits.append(selected_y) 

            sorted_profits = np.sort(local_profits)

            thresholds = [0.1, 0.2, 0.3]
            profit_min_percent = {}
            for threshold in thresholds:
                idx = int(threshold * len(sorted_profits))
                profit_min_percent[threshold] = sorted_profits[idx]

            selected_embedding = torch.sum(decision_prob.unsqueeze(2) * self.model.embedding.weight[:4, :], dim=1)

            predicted_tokens = self.model(input_id[:, :feature_dim], selected_embedding, ori_input[:, feature_dim + 1:])

            time_sum += predicted_tokens[-1].argmax(dim=1).sum().item()
            time_count += len(predicted_tokens[-1])

        profit = profit_sum / profit_count if profit_count > 0 else 0
        on_time_ratio = time_sum / time_count if time_count > 0 else 0

        return profit, on_time_ratio, profit_min_percent, time.time() - t



    def test(self, mode):

        chunk_size = int(self.env.args.batch_size // 1.5)
        self.model.eval() 
        t = time.time()

        if mode == 'val':
            input_id = self.val_inputs
        else:
            input_id = self.test_inputs

        
        
        correct_preds = 0  
        total_samples = 0

        ori_input = input_id.to(self.env.device)
        input_id = self.scaler.transform(input_id)
        input_id = torch.FloatTensor(input_id).to(self.env.device)

        feature_dim = len(feature_list.product_info[self.env.args.dataset] + feature_list.order_info[self.env.args.dataset] +\
                           feature_list.customer_info[self.env.args.dataset] + feature_list.shipping_info[self.env.args.dataset] )
        label_dim =  len(feature_list.label[self.env.args.dataset])

        ori_label_value_counts = {j: {} for j in range(label_dim)}

        
        label_value_counts = {j: {} for j in range(label_dim)}
        with torch.no_grad():
            
            correct_preds = [0] * label_dim  
            total_samples = [0] * label_dim  

            
            for i in range(0, len(input_id), chunk_size):
                
                input_chunk = input_id[i:i + chunk_size]
                ori_chunk = ori_input[i:i + chunk_size]

                
                for j in range(label_dim):
                    for value in ori_chunk[:, feature_dim + j + 1].cpu().numpy():
                        if value not in ori_label_value_counts[j]:
                            ori_label_value_counts[j][value] = 0
                        ori_label_value_counts[j][value] += 1
                    

                
                predicted_tokens = self.model(input_chunk[:,:feature_dim], ori_chunk[:,feature_dim].long(), ori_chunk[:,feature_dim+1:])

                
                class_labels = ori_chunk[:, -label_dim:].long().to(self.env.device)

                
                
                




                
                for j in range(label_dim):
                    predicted = torch.argmax(predicted_tokens[j], dim=1)  
                    correct_preds[j] += (predicted == class_labels[:, j]).sum().item()  
                    total_samples[j] += len(class_labels[:, j])  
                    for value in predicted.cpu().numpy():
                        if value not in label_value_counts[j]:
                            label_value_counts[j][value] = 0
                        label_value_counts[j][value] += 1

        
        accuracies = [correct_preds[j] / total_samples[j] for j in range(label_dim)]
        
        
        

        
        
        
        return accuracies, time.time() - t



    def save_ckpt(self, path):
        torch.save(self.model.state_dict(), path)

    def save_model(self, current_epoch, mode):
        model_state_file = os.path.join(
            self.env.CKPT_PATH, f'{self.env.suffix}_epoch{current_epoch}.pth')
        self.save_ckpt(model_state_file)
        if mode == 'sim':
            best_epoch = self.best_epoch
        else:
            best_epoch = self.best_dm_epoch
        if current_epoch != best_epoch:
            old_model_state_file = os.path.join(
                self.env.CKPT_PATH, f'{self.env.suffix}_epoch{best_epoch}.pth')
            if os.path.exists(old_model_state_file):
                os.system('rm {}'.format(old_model_state_file))

    def step(self, state, action):
        with torch.no_grad():
            state_tensor = torch.FloatTensor(state).unsqueeze(0).to(self.env.device)
            action_tensor = torch.LongTensor([action]).to(self.env.device)

            
            predicted_tokens = self.forward(state_tensor, action_tensor, state_tensor)
            profit = predicted_tokens[0, 0].item()
            on_time_ratio = predicted_tokens[0, 1].item()

        
        reward = profit + self.env.beta * on_time_ratio

        
        next_state = None  

        
        self.env.index += 1
        done = self.env.index >= len(self.env.loader.test_inputs)

        info = {"profit": profit, "on_time": on_time_ratio}
        return next_state, reward, done, info
    
    def step(self):
        self.model.eval()  
        self.value_network.eval()
        t = time.time()
        self.env.index = 0
        input_id = self.train_inputs

        ori_input = input_id.to(self.env.device)


        input_id = torch.FloatTensor(input_id).to(self.env.device)
        feature_dim = len(feature_list.product_info[self.env.args.dataset] + feature_list.order_info[self.env.args.dataset] +\
                        feature_list.customer_info[self.env.args.dataset] + feature_list.shipping_info[self.env.args.dataset])

        
        self.cost_dic_data = self.cost_dic[:, :-1]  
        self.cost_dic_y = self.cost_dic[:, -1]  

        
        index = faiss.IndexFlatL2(self.cost_dic_data.shape[1])  
        index.add(self.cost_dic_data)  

        profit_sum = 0  
        profit_count = 0  
        time_sum = 0  
        time_count = 0  
        local_profits = []  

        state = input_id[:, :feature_dim]

        with torch.no_grad():
            value_network_output = self.value_network(state)
            decision_prob_value =(F.softmax(value_network_output, dim=1) == F.softmax(value_network_output, dim=1).max(dim=1, keepdim=True).values).float()  
            decision_prob = decision_prob_value

            
            action = decision_prob_value.argmax(dim=1).squeeze()  
            
            
            

            query_vectors = np.array([
                [
                    ori_input[i, feature_list.retrieva_index[self.env.args.dataset][0]].cpu().item(),
                    ori_input[i, feature_list.retrieva_index[self.env.args.dataset][1]].cpu().item(),
                    action[i].item()
                ]
                for i in range(len(state))
            ], dtype='float32')

            _, nearest_indices = index.search(query_vectors, 1)  
            nearest_samples = self.cost_dic_data[nearest_indices.flatten()].cpu().numpy()  

            profit, on_time_ratio, reward = [], [], []


            for idx, (query, nearest) in enumerate(zip(query_vectors, nearest_samples)):
                if np.array_equal(query, nearest):
                    selected_y = self.cost_dic_y[nearest_indices[idx, 0]]
                else:
                    selected_y = torch.tensor(self.avg_profit)[action[idx].cpu()]

                profit.append(selected_y)



            selected_embedding = torch.sum(decision_prob.unsqueeze(2) * self.model.embedding.weight[:4, :], dim=1)

            predicted_tokens = self.model(input_id[:, :feature_dim], selected_embedding, ori_input[:, feature_dim + 1:])

            
            time_sum += predicted_tokens[-1].argmax(dim=1).sum().item()
            time_count += len(predicted_tokens[-1])


        
        
        
        on_time_ratio = predicted_tokens[-1].argmax(dim=1).tolist()

        for i in range(len(on_time_ratio)):
            reward.append(on_time_ratio[i] + profit[i])

        
        next_state = None

        done = [False * len(on_time_ratio)]
        done[-1] = True

        info = {"profit": profit, "on_time": on_time_ratio}
        return next_state, reward, done, info
    
