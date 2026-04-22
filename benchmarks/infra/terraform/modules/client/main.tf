resource "google_compute_instance" "this" {
  name         = "${var.name_prefix}-client"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["bench-node", "client"]
  labels       = merge(var.labels, { role = "client" })

  boot_disk {
    initialize_params {
      image = var.boot_image
      size  = 50
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = var.subnet_self_link
    access_config {}
  }

  metadata = {
    repo-url = var.repo_url
    repo-ref = var.repo_ref
  }

  metadata_startup_script = file("${path.module}/startup.sh")

  service_account {
    scopes = ["cloud-platform"]
  }
}
