# Slide Image Processor 1.0.2

A Fiji plugin that turns whole-slide scans of tissue sections into one
cropped, numbered, correctly oriented image per section.

It runs in three steps, all started from one launcher:

| Step | Tool | What it does |
|---|---|---|
| 1 | Exposure & rotation | Builds an image pyramid for fast viewing, sets the display range and pseudo-color of each channel, rotates / flips whole slides. Image export is optional. |
| 2 | Section Cropper | Detects the sections on every slide, lets you edit them (draw, wand, split, merge, delete, reorder, sample names) and exports one file per section. |
| 3 | Section Orienter | Flip / rotate tags per section, sample by sample, then writes the re-oriented series. |

## Requirements

- Fiji. Jython and Bio-Formats come with it; nothing else is needed.
- Tested with Fiji on Windows 11 (ImageJ 1.54p, Java 8, Jython 2.7.4). macOS and Linux have not been tested.
- Enough free disk space next to the images for the pyramid (folder `.misp_workspace`).

## Installation

1. Close Fiji.
2. Unzip this archive. It contains this README and a `sjleen` folder.
3. Copy the `sjleen` folder into the `plugins` folder of Fiji:
   - Windows / Linux: `Fiji.app/plugins/`
   - macOS: right-click `Fiji.app` > **Show Package Contents**, then `plugins/`

   Writing there needs permission to change the Fiji folder (for example, Fiji under `C:\Program Files` needs an administrator).
4. Start Fiji and run **Plugins > sjleen > Slide Image Processor v1.0.2**.

Only the launcher (`Slide_Image_Processor_v1.0.2.py`) appears in the menu; the three tools open from it.

## Updating

1. Close Fiji.
2. In `plugins/sjleen/`, delete:
   - the folder `Slide_Image_Processor_lib` (it holds compiled `$py.class` files that could keep running the old code)
   - the old launcher `Slide_Image_Processor_v<old>.py` (otherwise both versions appear in the menu)
3. Copy the new `sjleen` folder in as above and start Fiji.

Other files you keep in `plugins/sjleen/` are not touched. Projects and their pyramids are kept.

## Quick start

1. In the launcher, choose the folder that holds the slide images. Sub-folders are included.
2. **Step 1:** check the images, set the exposure of each channel, rotate slides if needed, then **Save to project**.
3. **Step 2:** **Detect selected slides**, fix the sections, set sample names, **Save to project**, then **Files > Export files...**.
4. **Step 3:** go through the samples, flip / rotate where needed, **Validate sample**, then **Files > Export files...**.

Under each launcher button, a status list shows what that step has produced: green = done, red = needed for the next step, gray = optional or not done yet.

## File names

The channel is read from the file names, for example `01_DAPI_1-6x16.tif` and `01_647_1-6x16.tif` form one slide with two channels. Name pieces are separated by `_` (each one, so `__` holds an empty piece) or spaces; `-` stays inside a piece. Files that hold several channels (multi-channel TIFF, RGB) are split into their channels automatically. The same file name may appear in different sub-folders.

## Outputs

All results stay inside the project folder.

```
<project>/
  .section_pipeline/project.json      all settings, sections and tags (the project)
  .misp_workspace/                    image pyramid (can be deleted; rebuilt by step 1)
  sections_<date>/                    step 2 export
    merged_8bit_color_JPG/<sub-folder>/001_<sample>_<slide>_Merge.jpg
    single_orig_gray_TIF/<sub-folder>/...
    ROIs/<sub-folder>/<slide>_sections.zip   (optional) ImageJ ROI set per slide
    sections.csv, jobs.csv, SectionCropper__..._export parameters.md
  oriented_sections_<date>/           step 3 export (same layout)
```

- Every export writes a `..._export parameters.md` with all the settings used.
- **Ignore subfolders** (step 1 and step 2 export) puts all files of a type in one folder instead of following the source sub-folders.
- In step 3, unchecking **Write all files to a new folder** renames the step-2 folder to `oriented_<name>` and rewrites only the turned images in place.
- JPG quality is 95, TIF is uncompressed. In step 3, JPGs are re-oriented from their TIF twin when one exists, so they are compressed only once.
- `merged_orig_color` TIF keeps every channel (raw values, LUT, display range) as a composite image; `merged_8bit_color` is an RGB picture.
- **ROI sets** (step 2 export option): each slide's section outlines as an ImageJ ROI set (`.zip`, open with the ROI Manager), named `NNN_sample`, in the pixel coordinates of the original slide image. With no image output ticked, only the ROI sets are written (a few seconds).

## Notes

- **Pixel size.** Detection lengths are in µm by default. The pixel size is read from the image files (TIFF calibration / OME metadata). If the files carry none, step 2 asks for it (Detection > pixel size), or switch the lengths to px.
- **Skip building and using L1** (launcher, on by default) makes the pyramid build much faster and smaller, at a small cost when viewing at mid zoom.
- **Sample names changed after an export?** Use step 2 **Files > Update names...**. It renames the exported files (step 2 and step 3) without cropping again. The launcher warns when names are out of date.
- **Touching sections.** Step 2 cuts sections that touch at a narrow neck (Touching > depth) and splits blobs much larger than a typical section (Touching > size x). Anything still merged can be split with the **Split** tool.
- **Step 3 keys.** Default shortcuts include A / S (rotate 1 degree counter-clockwise / clockwise); change any of them with **Keys...**.

## Version

- **1.0.2** (2026-11-04)
  - File names: every `_` separates pieces, so `__` counts as an empty piece, and `-` no longer splits (`s1-2`, `1-4x4` stay one piece). Folders that mix such names now load. Slide IDs of existing projects are unchanged.
- **1.0.1** (2026-10-20)
  - Step 1: the level (min / max) setting now applies to 8-bit images, in the viewer and in 8-bit / merged exports.
  - Step 1: channel detection from the file names no longer guesses when the name pattern allows several readings (for example 3 slides x 4 channels): channel-like names (DAPI, GFP, Cy5, 488, C1 ...) decide, otherwise you are asked.
  - Step 1: Apply / Auto / Export buttons respond to every click (trackpads).
  - Step 2: ROI sets export option.
  - Step 3: 1-degree rotation shortcuts (A / S by default).
- **1.0.0** (2026-10-12): first release.
