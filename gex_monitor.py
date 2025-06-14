import boto3
import os
import matplotlib
matplotlib.use('Agg') # Use Agg backend for non-GUI environments
import matplotlib.pyplot as plt
# ... (rest of your existing code) ...

def calculate_gamma_exposure():
    # ... (your existing GEX calculation and plotting code) ...

    try:
        # ... (plot generation) ...

        output_filename = "latest_gex_chart.png" # Consistent filename
        temp_filepath = f"/tmp/{output_filename}" # Save to a temporary directory

        plt.savefig(temp_filepath)
        plt.close() # Close the plot to free memory

        # --- ADDED: S3 Upload ---
        s3 = boto3.client('s3')
        bucket_name = 'your-gex-charts-bucket' # <<< REPLACE WITH YOUR S3 BUCKET NAME
        s3.upload_file(temp_filepath, bucket_name, output_filename,
                       ExtraArgs={'ContentType': 'image/png', 'ACL': 'public-read'}) # Make it publicly readable

        print(f"Plot saved and uploaded to S3: s3://{bucket_name}/{output_filename}")
        os.remove(temp_filepath) # Clean up local temp file
        # --- END ADDED ---

        print("Data collection cycle completed successfully.")

    except Exception as e:
        print(f"Error generating plot or uploading to S3: {e}")
        import traceback
        traceback.print_exc()
        print("Cycle completed with errors.")
# ... (rest of your main loop) ...
