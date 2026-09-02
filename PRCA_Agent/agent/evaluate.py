"""
Evaluation and visualization for FFM-Nano.
Generate trajectory predictions, embedding visualizations, and tactical similarity maps.
"""
import os
import sys
import torch
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.manifold import TSNE
from sklearn.decomposition import PCA

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from models.ffm_nano import FFMNano

def load_model(checkpoint_path: str, device="cuda"):
    """Load trained model from checkpoint."""
    ckpt = torch.load(checkpoint_path, map_location=device)
    config = ckpt.get("config", {
        "d_model": 128, "n_layers": 4, "n_heads": 4,
        "n_latents": 16, "d_ff": 512, "dropout": 0.1, "embed_dim": 64
    })

    model = FFMNano(**config).to(device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    print(f"Loaded checkpoint from epoch {ckpt.get('epoch', 'unknown')}")
    print(f"Val MSE: {ckpt.get('val_mse', 'unknown')}")
    return model

@torch.no_grad()
def predict_trajectory(model, sequence: torch.Tensor, timestamps: torch.Tensor, 
                       mask_indices: list, device="cuda"):
    """
    Predict masked frames in a sequence.
    sequence: [1, T, E, 5]
    mask_indices: list of frame indices to mask
    Returns: predictions, ground truth
    """
    seq = sequence.clone().to(device)
    timestamps = timestamps.to(device)

    mask = torch.zeros(1, seq.shape[1], dtype=torch.bool, device=device)
    mask[0, mask_indices] = True

    out = model(seq, timestamps, mask)
    pred = out["trajectory_pred"]

    return pred.cpu(), seq.cpu()

def visualize_prediction(sequence: np.ndarray, pred: np.ndarray, 
                         mask_indices: list, save_path: str = None):
    """Visualize predicted vs actual trajectories for masked frames."""
    fig, axes = plt.subplots(2, 3, figsize=(15, 10))
    axes = axes.flatten()

    # Plot a few entities: ball + 3 players
    entities_to_plot = [0, 1, 5, 12]  # ball, home1, home5, away1
    colors = ['red', 'blue', 'cyan', 'orange']
    labels = ['Ball', 'Home 1', 'Home 5', 'Away 1']

    for idx, frame_idx in enumerate(mask_indices[:6]):
        ax = axes[idx]
        ax.set_xlim(0, 105)
        ax.set_ylim(0, 68)
        ax.set_aspect('equal')
        ax.set_title(f"Frame {frame_idx} (Masked)")

        # Draw pitch outline
        ax.plot([0, 105, 105, 0, 0], [0, 0, 68, 68, 0], 'k-', linewidth=2)
        ax.plot([52.5, 52.5], [0, 68], 'k--', alpha=0.5)  # Halfway line

        for e_idx, color, label in zip(entities_to_plot, colors, labels):
            # Ground truth
            gt_x, gt_y = sequence[0, frame_idx, e_idx, 0], sequence[0, frame_idx, e_idx, 1]
            pred_x, pred_y = pred[0, frame_idx, e_idx, 0], pred[0, frame_idx, e_idx, 1]

            ax.scatter(gt_x, gt_y, c=color, marker='o', s=100, alpha=0.8, edgecolors='black')
            ax.scatter(pred_x, pred_y, c=color, marker='x', s=100, linewidths=3)

            # Arrow from pred to gt
            ax.annotate("", xy=(gt_x, gt_y), xytext=(pred_x, pred_y),
                       arrowprops=dict(arrowstyle="->", color=color, alpha=0.6))

        if idx == 0:
            ax.scatter([], [], c='black', marker='o', s=100, label='Ground Truth')
            ax.scatter([], [], c='black', marker='x', s=100, label='Prediction')
            ax.legend(loc='upper right')

    plt.tight_layout()
    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"Saved visualization to: {save_path}")
    plt.show()

@torch.no_grad()
def extract_embeddings(model, sequences: torch.Tensor, timestamps: torch.Tensor, 
                       device="cuda", batch_size=8):
    """Extract temporal embeddings for a set of sequences."""
    embeddings = []
    model.eval()

    for i in range(0, len(sequences), batch_size):
        batch = sequences[i:i+batch_size].to(device)
        ts = timestamps[i:i+batch_size].to(device)
        out = model(batch, ts)
        # Pool over time
        emb = out["temporal_emb"].mean(dim=1).cpu()  # [B, D]
        embeddings.append(emb)

    return torch.cat(embeddings, dim=0).numpy()

def visualize_embeddings(embeddings: np.ndarray, labels: np.ndarray = None, 
                         method="tsne", save_path: str = None):
    """Visualize embedding space with t-SNE or PCA."""
    if method == "tsne":
        reducer = TSNE(n_components=2, random_state=42, perplexity=min(30, len(embeddings)-1))
        title = "t-SNE of FFM-Nano Embeddings"
    else:
        reducer = PCA(n_components=2)
        title = "PCA of FFM-Nano Embeddings"

    coords = reducer.fit_transform(embeddings)

    plt.figure(figsize=(10, 8))
    if labels is not None:
        scatter = plt.scatter(coords[:, 0], coords[:, 1], c=labels, cmap="tab10", alpha=0.7, s=50)
        plt.colorbar(scatter, label="Match ID")
    else:
        plt.scatter(coords[:, 0], coords[:, 1], alpha=0.7, s=50)

    plt.title(title)
    plt.xlabel("Dim 1")
    plt.ylabel("Dim 2")
    plt.grid(True, alpha=0.3)

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"Saved embedding plot to: {save_path}")
    plt.show()

def compute_similarity_matrix(embeddings: np.ndarray) -> np.ndarray:
    """Compute cosine similarity matrix between embeddings."""
    # Normalize
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    embeddings_norm = embeddings / (norms + 1e-8)
    sim = np.dot(embeddings_norm, embeddings_norm.T)
    return sim

def plot_similarity_heatmap(sim_matrix: np.ndarray, save_path: str = None):
    """Plot similarity matrix heatmap."""
    plt.figure(figsize=(10, 8))
    sns.heatmap(sim_matrix, cmap="viridis", square=True, 
                xticklabels=False, yticklabels=False, cbar_kws={"label": "Cosine Similarity"})
    plt.title("Tactical Sequence Similarity Matrix")
    plt.tight_layout()
    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"Saved similarity heatmap to: {save_path}")
    plt.show()

def main():
    import glob

    CHECKPOINT = "./checkpoints/ffm_nano_best.pt"
    DATA_PATH = "./data/processed/metrica_sequences.npz"
    OUTPUT_DIR = "./outputs"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    # Load model
    print("\nLoading model...")
    model = load_model(CHECKPOINT, device)

    # Load data
    print("Loading data...")
    data = np.load(DATA_PATH)
    val_seqs = torch.from_numpy(data["val"]).float()
    timestamps = torch.arange(val_seqs.shape[1]).unsqueeze(0).repeat(len(val_seqs), 1) * 0.04

    # 1. Trajectory prediction visualization
    print("\nGenerating trajectory predictions...")
    sample_seq = val_seqs[0:1]
    sample_ts = timestamps[0:1]
    mask_indices = [20, 21, 22, 40, 41, 42, 60, 61, 62]
    pred, gt = predict_trajectory(model, sample_seq, sample_ts, mask_indices, device)
    visualize_prediction(gt.numpy(), pred.numpy(), mask_indices[:6], 
                        save_path=os.path.join(OUTPUT_DIR, "trajectory_pred.png"))

    # 2. Embedding extraction
    print("\nExtracting embeddings...")
    n_samples = min(200, len(val_seqs))
    embeddings = extract_embeddings(model, val_seqs[:n_samples], timestamps[:n_samples], device)

    # 3. Visualization
    print("\nGenerating visualizations...")
    labels = np.repeat([0, 1, 2], n_samples // 3)[:n_samples]  # Approximate match labels
    visualize_embeddings(embeddings, labels, method="tsne", 
                        save_path=os.path.join(OUTPUT_DIR, "embeddings_tsne.png"))

    # 4. Similarity matrix
    sim_matrix = compute_similarity_matrix(embeddings[:50])
    plot_similarity_heatmap(sim_matrix, 
                           save_path=os.path.join(OUTPUT_DIR, "similarity_matrix.png"))

    print(f"\n✅ All outputs saved to: {OUTPUT_DIR}")

if __name__ == "__main__":
    main()
