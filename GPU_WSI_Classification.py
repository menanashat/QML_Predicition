import os
import cv2
import torch
import numpy as np
import streamlit as st
import openslide
import pandas as pd
from PIL import Image, ImageDraw
from torchvision import transforms
from skimage.filters import threshold_otsu
from skimage.morphology import closing, square
from skimage.measure import label, regionprops
from collections import Counter
from torchvision.models import convnext_tiny

# -------------------------
# CONFIGURATION
# -------------------------
UPLOAD_DIR = "./uploaded_files"
OUTPUT_DIR = "./tiles_output"
RESULTS_CSV = "classification_results.csv"
TILE_SIZE = 256
TISSUE_THRESHOLD = 0.5
MODEL_PATH = "ConvNeXt_best_model.pth"  # Must be in repo

CLASS_NAMES = ["Tumor Cells", "Mitosis", "Karyorrhexis", "Stroma"]
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# -------------------------
# LOAD MODEL
# -------------------------
def load_model():
    model = convnext_tiny(num_classes=20)
    state_dict = torch.load(MODEL_PATH, map_location=device)
    state_dict = {k: v for k, v in state_dict.items() if "classifier.2" not in k}
    model.load_state_dict(state_dict, strict=False)
    model.classifier[2] = torch.nn.Linear(in_features=768, out_features=4)
    model.to(device)
    model.eval()
    return model

model = load_model()

transform = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
])

# -------------------------
# DETECT TISSUE REGIONS
# -------------------------
def detect_tissue_regions(slide):
    thumbnail = slide.get_thumbnail((1024, 1024))
    thumbnail_gray = np.array(cv2.cvtColor(np.array(thumbnail), cv2.COLOR_RGB2GRAY))
    threshold = threshold_otsu(thumbnail_gray)
    binary_mask = thumbnail_gray < threshold
    binary_mask = closing(binary_mask, square(5))
    labeled_mask = label(binary_mask)
    return labeled_mask

# -------------------------
# EXTRACT TILES
# -------------------------
def extract_tiles(slide, labeled_mask, downsample_factor, output_subdir):
    tiles_extracted = []
    slide_w, slide_h = slide.dimensions

    for region in regionprops(labeled_mask):
        minr, minc, maxr, maxc = region.bbox
        min_x, min_y = int(minc * downsample_factor), int(minr * downsample_factor)
        max_x, max_y = int(maxc * downsample_factor), int(maxr * downsample_factor)

        for x in range(min_x, max_x, TILE_SIZE):
            for y in range(min_y, max_y, TILE_SIZE):
                if x + TILE_SIZE > slide_w or y + TILE_SIZE > slide_h:
                    continue

                tile = slide.read_region((x, y), 0, (TILE_SIZE, TILE_SIZE)).convert("RGB")
                tile_np = np.array(tile)
                gray_tile = cv2.cvtColor(tile_np, cv2.COLOR_RGB2GRAY)
                tissue_ratio = np.sum(gray_tile < threshold_otsu(gray_tile)) / (TILE_SIZE * TILE_SIZE)

                if tissue_ratio > TISSUE_THRESHOLD:
                    tile_path = os.path.join(output_subdir, f"tile_{x}_{y}.jpg")
                    tile.save(tile_path)
                    tiles_extracted.append(tile_path)

    return tiles_extracted

# -------------------------
# PROCESS SVS FILE
# -------------------------
def process_svs_file(svs_path):
    filename = os.path.basename(svs_path).replace(".svs", "")
    output_subdir = os.path.join(OUTPUT_DIR, filename)
    os.makedirs(output_subdir, exist_ok=True)

    slide = openslide.OpenSlide(svs_path)
    labeled_mask = detect_tissue_regions(slide)
    thumb_w, thumb_h = 1024, 1024
    slide_w, slide_h = slide.dimensions
    downsample_factor = slide_w / thumb_w

    tile_paths = extract_tiles(slide, labeled_mask, downsample_factor, output_subdir)
    return tile_paths, output_subdir, downsample_factor

# -------------------------
# CLASSIFY TILES
# -------------------------
def classify_tiles(model, tile_paths):
    tile_predictions = {}
    tile_distributions = {}
    for tile_path in tile_paths:
        img = Image.open(tile_path)
        img_tensor = transform(img).unsqueeze(0).to(device)
        with torch.no_grad():
            output = model(img_tensor)
            probabilities = torch.nn.functional.softmax(output, dim=1).squeeze().cpu().numpy()
            predicted_index = torch.argmax(output, dim=1).item()
        tile_predictions[tile_path] = CLASS_NAMES[predicted_index]
        tile_distributions[tile_path] = {CLASS_NAMES[i]: round(probabilities[i] * 100, 2) for i in range(len(CLASS_NAMES))}
    return tile_predictions, tile_distributions

# -------------------------
# AGGREGATE PREDICTIONS
# -------------------------
def aggregate_predictions(tile_predictions):
    class_counts = Counter(tile_predictions.values())
    total_tiles = sum(class_counts.values())
    class_percentages = {cls: round((count / total_tiles) * 100, 2) for cls, count in class_counts.items()}
    most_common_class = class_counts.most_common(1)[0][0]
    return most_common_class, class_percentages

# -------------------------
# SAVE RESULTS TO CSV
# -------------------------
def save_results_to_csv(tile_predictions, tile_distributions, slide_name):
    results = []
    selected_classes = ["Tumor Cells", "Mitosis", "Karyorrhexis", "Stroma"]
    wsi_sums = {cls: 0 for cls in selected_classes}
    wsi_counts = {cls: 0 for cls in selected_classes}
    wsi_cell_counts = {cls: 0 for cls in selected_classes}
    total_tiles = len(tile_predictions)

    for tile_path, predicted_class in tile_predictions.items():
        row = {"Tile Name": tile_path, "Predicted Class": predicted_class, "Slide Name": slide_name}
        filtered_distributions = {}
        for cls in selected_classes:
            percentage = tile_distributions[tile_path].get(cls, 0)
            estimated_cell_count = int(round(percentage * TILE_SIZE * TILE_SIZE / 10000))
            filtered_distributions[f"{cls} (%)"] = percentage
            filtered_distributions[f"{cls} Count"] = estimated_cell_count
            wsi_sums[cls] += percentage
            wsi_cell_counts[cls] += estimated_cell_count
            if percentage > 0:
                wsi_counts[cls] += 1
        row.update(filtered_distributions)
        is_tumor_positive = filtered_distributions["Tumor Cells (%)"] > 0
        row["Final Classification"] = "Tumor Positive" if is_tumor_positive else "Tumor Negative"
        results.append(row)

    wsi_percentages = {cls: round(wsi_sums[cls] / total_tiles, 2) for cls in selected_classes}
    wsi_tumor_positive = wsi_percentages["Tumor Cells"] > 0
    wsi_final_classification = "Tumor Positive" if wsi_tumor_positive else "Tumor Negative"
    wsi_summary = {"Tile Name": "Overall WSI Summary", "Predicted Class": wsi_final_classification, "Slide Name": slide_name, "Final Classification": wsi_final_classification}
    for cls in selected_classes:
        wsi_summary[f"{cls} (%)"] = wsi_percentages[cls]
        wsi_summary[f"{cls} Count"] = wsi_cell_counts[cls]
    results.append(wsi_summary)

    df = pd.DataFrame(results)
    df.to_csv(RESULTS_CSV, index=False)

# -------------------------
# MARK TUMOR REGIONS
# -------------------------
def mark_tumor_regions(slide, tumor_tiles, downsample_factor):
    try:
        thumbnail = slide.get_thumbnail((1024, 1024))
        draw = ImageDraw.Draw(thumbnail)
        for tile_path in tumor_tiles:
            parts = os.path.basename(tile_path).replace("tile_", "").replace(".jpg", "").split("_")
            if len(parts) != 2:
                continue
            x, y = map(int, parts)
            x_thumb, y_thumb = int(x / downsample_factor), int(y / downsample_factor)
            box_size = int(256 / downsample_factor)
            draw.rectangle([x_thumb, y_thumb, x_thumb + box_size, y_thumb + box_size], outline="red", width=2)
        return thumbnail
    except Exception as e:
        st.error(f"❌ Error in mark_tumor_regions: {e}")
        return None

# -------------------------
# MARK ALL CELL TYPES
# -------------------------
def mark_all_cell_types(slide, tile_predictions, downsample_factor):
    try:
        thumbnail = slide.get_thumbnail((1024, 1024))
        draw = ImageDraw.Draw(thumbnail)
        cell_colors = {
            "Tumor Cells": "red",
            "Mitosis": "blue",
            "Karyorrhexis": "purple",
            "Stroma": "green",
        }
        for tile_path, cell_type in tile_predictions.items():
            parts = os.path.basename(tile_path).replace("tile_", "").replace(".jpg", "").split("_")
            if len(parts) != 2:
                continue
            x, y = map(int, parts)
            x_thumb, y_thumb = int(x / downsample_factor), int(y / downsample_factor)
            box_color = cell_colors.get(cell_type, "gray")
            box_size = int(256 / downsample_factor)
            draw.rectangle([x_thumb, y_thumb, x_thumb + box_size, y_thumb + box_size], outline=box_color, width=2)
        return thumbnail
    except Exception as e:
        st.error(f"❌ Error in mark_all_cell_types: {e}")
        return None

# -------------------------
# STREAMLIT UI
# -------------------------
st.title("🧪 Multi-Class Tumor Classification in Whole Slide Images (WSI) 🧪")
st.write("Upload an **SVS** file to classify the extracted tissue tiles.")

session_vars = ["svs_path", "tile_predictions", "tile_distributions", "class_distribution", "tumor_tiles", "mitosis_tiles", "karyorrhexis_tiles", "marked_thumbnail", "downsample_factor"]
for var in session_vars:
    if var not in st.session_state:
        st.session_state[var] = None
st.session_state.setdefault("class_distribution", {})

uploaded_file = st.file_uploader("Upload an SVS file", type=["svs"])

if uploaded_file is not None:
    if st.session_state.svs_path != os.path.join(UPLOAD_DIR, uploaded_file.name):
        st.session_state.svs_path = None
        st.session_state.tile_predictions = None
        st.session_state.tile_distributions = None
        st.session_state.class_distribution = {}
        st.session_state.tumor_tiles = []
        st.session_state.mitosis_tiles = []
        st.session_state.karyorrhexis_tiles = []

    if st.session_state.tile_predictions is None:
        svs_path = os.path.join(UPLOAD_DIR, uploaded_file.name)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        with open(svs_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        st.session_state.svs_path = svs_path
        st.success(f"✅ {uploaded_file.name} uploaded successfully!")

        st.write("🖼 Extracting tissue regions and tiling...")
        tile_paths, tile_folder, downsample_factor = process_svs_file(svs_path)
        st.session_state.downsample_factor = downsample_factor

        if not tile_paths:
            st.error("❌ No valid tissue-containing tiles found! Try another slide.")
        else:
            st.write(f"✅ Extracted **{len(tile_paths)}** tiles.")
            st.write("🔍 Running classification on extracted tiles...")
            tile_predictions, tile_distributions = classify_tiles(model, tile_paths)
            final_prediction, class_distribution = aggregate_predictions(tile_predictions)

            st.session_state.tile_predictions = tile_predictions
            st.session_state.tile_distributions = tile_distributions
            st.session_state.class_distribution = class_distribution
            st.session_state.tumor_tiles = [tile for tile, pred in tile_predictions.items() if pred == "Tumor Cells"]
            st.session_state.mitosis_tiles = [tile for tile, pred in tile_predictions.items() if pred == "Mitosis"]
            st.session_state.karyorrhexis_tiles = [tile for tile, pred in tile_predictions.items() if pred == "Karyorrhexis"]
            save_results_to_csv(tile_predictions, tile_distributions, uploaded_file.name)

# -------------------------
# DISPLAY CLASSIFICATION RESULTS
# -------------------------
if st.session_state.tile_predictions and st.session_state.class_distribution:
    st.write("### 📊 **Tile Class Distribution**")
    selected_classes = ["Tumor Cells", "Mitosis", "Karyorrhexis", "Stroma"]
    tile_class_counts = {cls: int(round(st.session_state.class_distribution.get(cls, 0) * TILE_SIZE * TILE_SIZE / 10000)) for cls in selected_classes}
    for cls, percentage in st.session_state.class_distribution.items():
        if cls in tile_class_counts:
            count = tile_class_counts[cls]
            st.write(f"🔹 {cls}: **{percentage}%** ({count} cells)")

    tumor_percentage = st.session_state.class_distribution.get("Tumor Cells", 0)
    mk_index = (st.session_state.class_distribution.get("Mitosis", 0) + st.session_state.class_distribution.get("Karyorrhexis", 0))
    has_tumor = tumor_percentage > 0
    slide_status = "Tumor Positive" if has_tumor else "Tumor Negative"

    st.write("### 🏆 **Final Whole-Slide Classification**")
    st.success(f"🩸 **Predicted Slide Class:** {slide_status} (Tumor Cells: {tumor_percentage}%)")
    st.write(f"🧬 **MK Index (Mitosis + Karyorrhexis):** {mk_index}%")
    st.download_button("📥 Download Classification Results", open(RESULTS_CSV, "rb"), "classification_results.csv", "text/csv")

# -------------------------
# CLASS DISTRIBUTION BUTTON
# -------------------------
with st.expander("📊 Class Distribution"):
    st.write("### 🔬 Full Class Breakdown")
    CLASS_COLORS = {
        "Tumor Cells": "#FF0000", "Mitosis": "#0000FF", "Karyorrhexis": "#00FF00", "Stroma": "#FFFF00",
        "Karyolysis": "#FFA500", "Necrosis": "#800080", "Inflammatory Cells": "#FFFFFF", "Blood Vessels": "#8B0000",
        "Fibroblasts": "#4682B4", "Macrophages": "#FF4500", "Epithelial Cells": "#9932CC", "Lymphocytes": "#DC143C",
        "Endothelial Cells": "#FFD700", "Connective Tissue": "#32CD32", "Basement Membrane": "#D2691E",
        "Apoptotic Bodies": "#DCDCDC", "Cytoplasmic Fragments": "#800080", "Granulocytes": "#1E90FF",
        "Mast Cells": "#FF8C00", "Adipose Tissue": "#FFFF00"
    }
    if st.session_state.class_distribution is None:
        st.warning("Class distribution is not available.")
    else:
        for cls, percentage in st.session_state.class_distribution.items():
            if cls not in ["Tumor Cells", "Mitosis", "Karyorrhexis", "Stroma"]:
                color = CLASS_COLORS.get(cls, "#000000")
                st.markdown(f"<span style='color:{color}; font-weight:bold;'>{cls}: {percentage}%</span>", unsafe_allow_html=True)

        if st.button("🖼 Show Class Distribution Map"):
            st.write("📍 **Visualizing Cell Type Distribution on WSI...**")
            if st.session_state.svs_path:
                try:
                    slide = openslide.OpenSlide(st.session_state.svs_path)
                    marked_thumbnail = mark_all_cell_types(slide, st.session_state.tile_predictions, st.session_state.downsample_factor)
                    if marked_thumbnail:
                        st.image(marked_thumbnail, caption="Class Distribution Map", use_column_width=True)
                    else:
                        st.error("❌ Failed to generate class distribution visualization.")
                except Exception as e:
                    st.error(f"❌ Error opening SVS file: {e}")
            else:
                st.error("❌ SVS file not found. Please re-upload.")

        if st.session_state.tumor_tiles and has_tumor and st.session_state.svs_path:
            if st.button("🔍 Check Tumor Regions"):
                st.write("🔬 Highlighting Tumor-Detected Regions...")
                try:
                    slide = openslide.OpenSlide(st.session_state.svs_path)
                    marked_thumbnail = mark_tumor_regions(slide, st.session_state.tumor_tiles, st.session_state.downsample_factor)
                    if marked_thumbnail:
                        st.image(marked_thumbnail, caption="Tumor Regions in WSI", use_column_width=True)
                    else:
                        st.error("❌ Failed to generate tumor region visualization.")
                except Exception as e:
                    st.error(f"❌ Error opening SVS file: {e}")
        else:
            st.warning("⚠️ No Tumor Cells detected. Visualization disabled.")

        st.write("🗺 **Color Legend:**")
        legend_html = "".join(f"<span style='color:{color}; font-weight:bold;'>⬤ {cls}</span>   |  " for cls, color in CLASS_COLORS.items())
        st.markdown(legend_html, unsafe_allow_html=True)
